"""Start/reuse the user's local backend through their ordinary Terminal session."""
import fcntl,json,subprocess,time,urllib.request
from pathlib import Path
base=Path.home()/'Library/Application Support/向上成长顾问'
c=json.loads((base/'desktop.json').read_text());data=Path(c['data'])
def ready():
    try:
        with urllib.request.urlopen('http://127.0.0.1:60128/api/identity',timeout=1) as r:s=json.load(r)
        return s.get('application')=='growth-desk' and s.get('data_id')==c['data_id']
    except Exception:return False
with (base/'startup.lock').open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX)
    if not ready():
        data.mkdir(parents=True,exist_ok=True)
        with (data/'desktop-service.log').open('ab') as log:
            child=subprocess.Popen([c['python'],str(Path(c['source'])/'app.py'),'--data-dir',str(data),'--port','60128'],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        for _ in range(30):
            if ready():break
            if child.poll() is not None:break
            time.sleep(.3)
        if not ready():
            if child.poll() is None:child.terminate()
            raise SystemExit('服务没有就绪，请检查私人数据目录的 desktop-service.log。')
print('成长工作台已启动，此窗口可以关闭。')
