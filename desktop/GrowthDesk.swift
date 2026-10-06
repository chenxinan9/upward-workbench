import AppKit
import WebKit

final class GrowthApp: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var process: Process?
    var tries = 0
    var started = false
    var config: [String:String] = [:]
    let endpoint = URL(string:"http://127.0.0.1:60128")!
    func applicationDidFinishLaunching(_ notification: Notification) {
        makeMenu()
        window = NSWindow(contentRect:NSRect(x:0,y:0,width:1260,height:860),styleMask:[.titled,.closable,.miniaturizable,.resizable],backing:.buffered,defer:false)
        window.title = "向上工作台"
        window.minSize = NSSize(width:760,height:580)
        window.setFrameAutosaveName("GrowthDeskMain")
        window.center()
        web = WKWebView(frame:window.contentView!.bounds)
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
    @objc func reload() { web.reload() }
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
        if u.absoluteString=="about:blank" || (u.scheme=="http" && u.host=="127.0.0.1" && u.port==60128) {decisionHandler(.allow);return}
        if ["https","http"].contains(u.scheme ?? "") {NSWorkspace.shared.open(u)}
        decisionHandler(.cancel)
    }
    func webView(_ webView:WKWebView,createWebViewWith configuration:WKWebViewConfiguration,for action:WKNavigationAction,windowFeatures:WKWindowFeatures)->WKWebView? {
        if let u=action.request.url,["https","http"].contains(u.scheme ?? "") {NSWorkspace.shared.open(u)}
        return nil
    }
}
let app=NSApplication.shared;let delegate=GrowthApp();app.delegate=delegate;app.setActivationPolicy(.regular);app.run()
