import AppKit
import WebKit
import Speech
import AVFoundation

final class GrowthApp: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var web: WKWebView!
    var process: Process?
    var tries = 0
    var started = false
    var config: [String:String] = [:]
    let audio = AVAudioEngine()
    var speechRequest: SFSpeechAudioBufferRecognitionRequest?
    var speechTask: SFSpeechRecognitionTask?
    var speechID: String?
    var audioTapInstalled = false
    var latestTranscript = ""
    let endpoint = URL(string:"http://127.0.0.1:60128")!
    func applicationDidFinishLaunching(_ notification: Notification) {
        makeMenu()
        window = NSWindow(contentRect:NSRect(x:0,y:0,width:1260,height:860),styleMask:[.titled,.closable,.miniaturizable,.resizable],backing:.buffered,defer:false)
        window.title = "向上工作台"
        window.delegate = self
        window.minSize = NSSize(width:760,height:580)
        window.setFrameAutosaveName("GrowthDeskMain")
        window.center()
        let webConfig = WKWebViewConfiguration()
        webConfig.userContentController.add(self, name:"upwardSpeech")
        webConfig.userContentController.addUserScript(WKUserScript(source:"window.upwardNativeSpeech = true;",injectionTime:.atDocumentStart,forMainFrameOnly:true))
        web = WKWebView(frame:window.contentView!.bounds, configuration:webConfig)
        web.autoresizingMask = [.width,.height]
        web.navigationDelegate = self; web.uiDelegate = self
        window.contentView?.addSubview(web)
        web.loadHTMLString("<html lang='zh-CN'><body style='font:20px system-ui;padding:60px;background:#f6f7fb;color:#253047'>正在打开你的成长工作台…</body></html>",baseURL:nil)
        window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps:true)
        let file = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/向上成长顾问/desktop.json")
        do { config = try JSONSerialization.jsonObject(with:Data(contentsOf:file)) as? [String:String] ?? [:]; probe() }
        catch { fail("桌面连接配置不存在。请重新运行本机安装脚本。") }
    }
    func makeMenu() {
        let bar=NSMenu();let item=NSMenuItem();bar.addItem(item)
        let app=NSMenu();app.addItem(withTitle:"退出向上工作台",action:#selector(NSApplication.terminate(_:)),keyEquivalent:"q");item.submenu=app
        let editItem=NSMenuItem();bar.addItem(editItem);let edit=NSMenu(title:"编辑");editItem.submenu=edit
        for (title,action,key) in [("撤销","undo:","z"),("剪切","cut:","x"),("拷贝","copy:","c"),("粘贴","paste:","v"),("全选","selectAll:","a")] { edit.addItem(withTitle:title,action:Selector(action),keyEquivalent:key) }
        let viewItem=NSMenuItem();bar.addItem(viewItem);let view=NSMenu(title:"查看");viewItem.submenu=view
        view.addItem(withTitle:"刷新工作台",action:#selector(reload),keyEquivalent:"r");NSApp.mainMenu=bar
    }
    @objc func reload() { stopSpeech(cancel:true);web.reload() }
    func windowWillClose(_ notification:Notification) {stopSpeech(cancel:true)}
    func speechEvent(_ state:String, text:String = "", message:String = "", final:Bool = false, id:String? = nil) {
        let payload:[String:Any] = ["state":state,"text":text,"message":message,"final":final,"request_id":id ?? speechID ?? ""]
        if let data=try? JSONSerialization.data(withJSONObject:payload),let json=String(data:data,encoding:.utf8) {
            web.evaluateJavaScript("window.dispatchEvent(new CustomEvent('upward-speech',{detail:\(json)}));",completionHandler:nil)
        }
    }
    func stopSpeech(cancel:Bool) {
        audio.stop()
        if audioTapInstalled {audio.inputNode.removeTap(onBus:0);audioTapInstalled=false}
        if cancel {speechTask?.cancel();speechTask=nil;speechRequest=nil;speechID=nil}
        else {
            speechRequest?.endAudio();speechEvent("transcribing")
            let endingID=speechID
            DispatchQueue.main.asyncAfter(deadline:.now()+12) {
                if let id=endingID,self.speechID==id {
                    if !self.latestTranscript.isEmpty {self.speechEvent("result",text:self.latestTranscript,final:true,id:id)}
                    else {self.speechEvent("error",message:"语音识别没有返回文字，请重试或使用系统听写",id:id)}
                    self.stopSpeech(cancel:true)
                }
            }
        }
    }
    func beginSpeech(_ id:String) {
        guard speechID == nil else {speechEvent("error",message:"上一段语音仍在处理",id:id);return}
        speechID=id;latestTranscript=""
        SFSpeechRecognizer.requestAuthorization { status in
            DispatchQueue.main.async {
                guard self.speechID == id else {return}
                guard status == .authorized else {self.speechEvent("error",message:"请在系统设置的隐私与安全中允许向上工作台使用语音识别",id:id);self.stopSpeech(cancel:true);return}
                AVCaptureDevice.requestAccess(for:.audio) { allowed in
                    DispatchQueue.main.async {
                        guard self.speechID == id else {return}
                        guard allowed else {self.speechEvent("error",message:"麦克风权限未开启，请在系统设置中允许向上工作台使用麦克风",id:id);self.stopSpeech(cancel:true);return}
                        self.recordSpeech(id)
                    }
                }
            }
        }
    }
    func recordSpeech(_ id:String) {
        guard let recognizer=SFSpeechRecognizer(locale:Locale(identifier:"zh-CN")),recognizer.isAvailable else {speechEvent("error",message:"系统中文语音识别当前不可用，请检查网络或使用系统听写",id:id);stopSpeech(cancel:true);return}
        let request=SFSpeechAudioBufferRecognitionRequest();request.shouldReportPartialResults=true
        // Prefer on-device recognition; otherwise macOS uses its speech service under the user's permission.
        if recognizer.supportsOnDeviceRecognition {request.requiresOnDeviceRecognition=true}
        speechRequest=request
        let input=audio.inputNode;let format=input.outputFormat(forBus:0)
        guard format.sampleRate>0,format.channelCount>0 else {speechEvent("error",message:"没有可用的麦克风输入",id:id);stopSpeech(cancel:true);return}
        input.installTap(onBus:0,bufferSize:1024,format:format) {buffer,_ in request.append(buffer)};audioTapInstalled=true
        speechTask=recognizer.recognitionTask(with:request) {result,error in
            DispatchQueue.main.async {
                guard self.speechID == id else {return}
                if let result=result {self.latestTranscript=result.bestTranscription.formattedString;self.speechEvent("result",text:self.latestTranscript,final:result.isFinal,id:id)}
                if result?.isFinal == true {self.stopSpeech(cancel:true);self.speechEvent("stopped",id:id)}
                else if let error=error {self.speechEvent("error",message:error.localizedDescription,id:id);self.stopSpeech(cancel:true)}
            }
        }
        do {audio.prepare();try audio.start();speechEvent("listening",id:id)}
        catch {speechEvent("error",message:error.localizedDescription,id:id);stopSpeech(cancel:true)}
        DispatchQueue.main.asyncAfter(deadline:.now()+60) {if self.speechID == id {self.stopSpeech(cancel:false)}}
    }
    func userContentController(_ userContentController:WKUserContentController,didReceive message:WKScriptMessage) {
        let origin=message.frameInfo.securityOrigin
        guard message.frameInfo.isMainFrame,origin.protocol=="http",origin.host=="127.0.0.1",origin.port==60128,
              let value=message.body as? [String:String] else {return}
        if value["action"]=="start",let id=value["request_id"],id.count<100 {beginSpeech(id)}
        else if value["action"]=="stop",value["request_id"]==speechID {stopSpeech(cancel:false)}
        else if value["action"]=="cancel",value["request_id"]==speechID {let id=speechID;stopSpeech(cancel:true);speechEvent("stopped",id:id)}
    }
    func applicationWillTerminate(_ notification:Notification) {stopSpeech(cancel:true)}
    func webView(_ webView:WKWebView,runOpenPanelWith parameters:WKOpenPanelParameters,initiatedByFrame frame:WKFrameInfo,completionHandler:@escaping([URL]?)->Void) {
        let panel=NSOpenPanel();panel.canChooseDirectories=false;panel.allowsMultipleSelection=parameters.allowsMultipleSelection
        panel.beginSheetModal(for:window) {response in completionHandler(response == .OK ? panel.urls : nil)}
    }
    func probe() {
        var req=URLRequest(url:endpoint.appendingPathComponent("api/identity"));req.timeoutInterval=1
        URLSession.shared.dataTask(with:req) { data,response,error in
            let dict = data.flatMap { try? JSONSerialization.jsonObject(with:$0) as? [String:Any] }
            DispatchQueue.main.async {
                if let http=response as? HTTPURLResponse,http.statusCode==200,dict?["application"] as? String == "growth-desk",dict?["data_id"] as? String == self.config["data_id"] { self.web.load(URLRequest(url:self.endpoint));return }
                if response != nil { self.fail("60128 端口不是这个私人数据目录的工作台。已停止连接，请先检查本机服务。");return }
                if !self.started { self.started=true;do {try self.startServer()}catch {self.fail("无法启动本地服务：\(error.localizedDescription)");return} }
                self.tries += 1
                if self.tries >= 30 { self.fail("本地服务未能就绪。请检查私人数据目录中的 desktop-service.log。");return }
                DispatchQueue.main.asyncAfter(deadline:.now()+0.4) {self.probe()}
            }
        }.resume()
    }
    func startServer() throws {
        guard let launcher=config["launcher"] else {throw NSError(domain:"配置缺少启动器",code:1)}
        guard NSWorkspace.shared.open(URL(fileURLWithPath:launcher)) else {throw NSError(domain:"无法打开本机启动器",code:2)}
    }
    func fail(_ message:String) {let alert=NSAlert();alert.messageText="暂时无法打开成长工作台";alert.informativeText=message;alert.addButton(withTitle:"知道了");alert.runModal()}
    func applicationShouldHandleReopen(_ sender:NSApplication,hasVisibleWindows flag:Bool)->Bool {window.makeKeyAndOrderFront(nil);return true}
    func webView(_ webView:WKWebView,decidePolicyFor action:WKNavigationAction,decisionHandler:@escaping(WKNavigationActionPolicy)->Void) {
        guard let u=action.request.url else {decisionHandler(.cancel);return}
        if u.absoluteString=="about:blank" || (u.scheme=="http" && u.host=="127.0.0.1" && u.port==60128) {if action.targetFrame?.isMainFrame == true {stopSpeech(cancel:true)};decisionHandler(.allow);return}
        if ["https","http"].contains(u.scheme ?? "") {NSWorkspace.shared.open(u)}
        decisionHandler(.cancel)
    }
    func webView(_ webView:WKWebView,createWebViewWith configuration:WKWebViewConfiguration,for action:WKNavigationAction,windowFeatures:WKWindowFeatures)->WKWebView? {
        if let u=action.request.url,["https","http"].contains(u.scheme ?? "") {NSWorkspace.shared.open(u)}
        return nil
    }
}
let app=NSApplication.shared;let delegate=GrowthApp();app.delegate=delegate;app.setActivationPolicy(.regular);app.run()
