codex打不开：
第一次打不开 — dev server 进程挂掉了（ps aux 查不到 runserver 进程），重启 bash scripts/devserver_8000.sh restart 就好了。

重启后页面 302 — Django 的 session 是内存级别的，重启 server 后旧 session 失效。浏览器拿着旧 cookie 访问，Django 不认识，就 302 跳转登录页。重新登录即可恢复。



# 重启服务器
bash /home/c/ccAPPS/scripts/devserver_8000.sh restart

# 查看服务器状态
bash /home/c/ccAPPS/scripts/devserver_8000.sh status

# 只看服务器还活着没
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/data/login/
