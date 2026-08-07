# STDWeb 本地部署说明（local-zh 分支）

本文档记录本机 STDWeb 部署的本地化改动、日常运维和更新流程。
代码位于 /home/ajst/Astro_Software/stdweb，所有本地改动都提交在 `local-zh` 分支。

## 目录

- [部署架构](#部署架构)
- [本地化改动（相对上游）](#本地化改动相对上游)
- [日常使用](#日常使用)
- [更新上游代码](#更新上游代码)
- [故障排查](#故障排查)

## 部署架构

| 组件 | 说明 | 管理方式 |
|---|---|---|
| Redis | 任务队列 broker（系统服务） | `sudo systemctl start/stop redis-server` |
| Celery worker | 后台处理，12 并发 | `start_stdweb.sh` / `stop_stdweb.sh` |
| Django runserver | Web 服务，仅监听 127.0.0.1:8000 | `start_stdweb.sh` / `stop_stdweb.sh` |
| conda 环境 | `stdweb`（Python 3.12.13） | 见下文 |

Python 环境：`/home/ajst/miniconda3/envs/stdweb/bin/python`（注意：**不要**用 `conda run`，
本机 conda 26.3.2 的 activate 钩子有 bug 会报错；直接用环境绝对路径）。

关键路径：

- 代码：`/home/ajst/Astro_Software/stdweb/`
- STDPipe（editable 安装）：`/home/ajst/Astro_Software/stdpipe/`
- 上传数据：`/home/ajst/Astro_Software/stdweb/data/`（.gitignore）
- 任务文件：`/home/ajst/Astro_Software/stdweb/tasks/`（.gitignore）
- 模板缓存：`/home/ajst/Astro_Software/stdweb/ps1_cache/`（.gitignore）
- 配置：`/home/ajst/Astro_Software/stdweb/.env`（.gitignore，含 SECRET_KEY）
- 日志：`celery.log`、`server.log`（stdweb 目录下）

## 本地化改动（相对上游）

全部提交在 `local-zh` 分支（commit `e7d0273`），master 保持与上游一致。

1. **全界面中文汉化**：21 个模板、forms.py（90 个字段标签）、views.py / views_tasks.py
   （58 条提示消息）、filters.py（新增 `state_cn` 状态过滤器）。天文学专有名词保留英文。
2. **安全收紧**：`stdweb/settings.py` 的 `ALLOWED_HOSTS` 改为从环境变量读取，
   默认 `127.0.0.1,localhost`（可在 .env 里用 `ALLOWED_HOSTS` 覆盖）。
3. **Files 删除功能**：文件浏览页新增删除按钮（`views.py` 的 `list_files` 增加 POST 处理 +
   `files.html` 模板）。安全设计：软链只删链接本身、普通文件有路径穿越防护、仅登录可用。
4. **首页上传选择 FITS 扩展层 (HDU)**：上传表单新增"FITS 扩展层 (HDU)"下拉
   （默认"自动（最后一个 HDU）"，可显式选 HDU 0-10）。`forms.py` 的 `UploadFileForm`
   新增 `ext` 字段；`views.py` 的 `upload_file` 在选定 HDU 时用
   `fits.getdata/getheader` 提取该扩展为单扩展 image.fits（保留其头信息）。
   注：处理端统一读 `fits.getdata(image.fits, -1)`（最后一个 HDU），多扩展 FITS
   若数据不在最后扩展，务必用本功能或 Files 导入（详情页"处理此扩展名"）选定正确扩展。
5. **Files 页面上传到数据区**：文件列表页顶部新增"上传到数据区"表单
   （`views.py` 新增 `upload_data` 视图 + `urls.py` 注册 `upload_data` 路由 +
   `files.html` 模板）。文件存入 DATA_PATH 但不创建任务，之后可在 Files 页
   点开预览各 HDU 并导入任务。同名文件拒绝上传（先删除再传）；文件名经
   `os.path.basename` 净化防路径穿越。
6. **运维脚本**：`start_stdweb.sh` / `stop_stdweb.sh`（celery 并发固定为 6，
   12 个 astropy worker 对 15GB 内存偏多）。

## 日常使用

```bash
# 启动（Redis + Celery + Django，仅本机可访问）
/home/ajst/Astro_Software/stdweb/start_stdweb.sh

# 停止（Redis 保持运行）
/home/ajst/Astro_Software/stdweb/stop_stdweb.sh

# 访问
# 浏览器打开 http://127.0.0.1:8000
```

登录凭据：admin 账号（密码见 .env 或 /tmp/stdweb_admin_cred.txt，若改过请自行记忆）。
改密码：

```bash
cd /home/ajst/Astro_Software/stdweb
/home/ajst/miniconda3/envs/stdweb/bin/python manage.py changepassword admin
```

多扩展 FITS 推荐流程（数据不在第一个 HDU 时）：

1. Files 页 → "上传到数据区"（或直接 `cp` 到 data/ 目录）
2. Files 页点开该文件 → 预览各 HDU（PRIMARY/SCI/WEIGHT…）→ 确认科学数据所在扩展
3. 点该扩展的"处理此扩展名"导入任务；或勾选多个文件"处理所选文件"批量导入
4. 主页直接上传时，可用"FITS 扩展层 (HDU)"下拉选定扩展（默认自动 = 最后一个 HDU）

### 启动脚本说明

`start_stdweb.sh` 会依次检查/启动：Redis（systemd）→ Celery worker → Django runserver，
并在就绪后输出访问地址。脚本用 `pgrep -f "[p]ython.*-m celery"` 精确匹配进程，
不会被自身命令行误判（[x] 技巧）。

## 更新上游代码

**必须全程在 local-zh 分支操作**（本地化改动都在这个分支里）：

```bash
cd /home/ajst/Astro_Software/stdweb

# 1. 切到 master，拉上游
git checkout master
git pull

# 2. 切回本地分支，合并上游
git checkout local-zh
git merge master

# 3. 处理冲突（如有）
#    上游若改动了汉化过的同一行会冲突，手动保留中文版本后：
#    git add <冲突文件> && git commit
```

合并后注意：

- 上游若新增了 Python 依赖：`/home/ajst/miniconda3/envs/stdweb/bin/pip install -r requirements.txt`
- 上游若改了数据库模型：`/home/ajst/miniconda3/envs/stdweb/bin/python manage.py migrate`
- 上游若改了 settings.py 且 ALLOWED_HOSTS 冲突：重新应用本地收紧（见上文第 2 条）
- 模板/代码改动由 runserver autoreload 自动生效，无需重启（.py 改动会自动重启进程）

### 环境重装（万一 conda 环境损坏）

```bash
# conda 插件有 bug，必须加 --solver classic
conda create -n stdweb python=3.12 pip -y --solver classic
# 先 pin Django <6（DRF 3.17 与 Django 6 不兼容），再装依赖
/home/ajst/miniconda3/envs/stdweb/bin/pip install "Django>=5.2,<6"
/home/ajst/miniconda3/envs/stdweb/bin/pip install -e /home/ajst/Astro_Software/stdpipe
/home/ajst/miniconda3/envs/stdweb/bin/pip install -r requirements.txt
```

外部二进制依赖（已装好）：SExtractor(/usr/local/bin/sex)、SCAMP、HOTPANTS
(/home/ajst/Astro_Software/hotpants/hotpants)、Astrometry.Net solve-field + 66GB index
(/home/ajst/Astro_Catalog/index_for_Astrometry/，配置 /etc/astrometry.cfg)。

## 故障排查

| 症状 | 处理 |
|---|---|
| 页面没样式/JS | runserver 必须带 `--insecure`（DEBUG=False 时不服务静态文件），start 脚本已带 |
| celery 进程假死 | `stop_stdweb.sh && start_stdweb.sh` |
| 任务一直排队不跑 | `redis-cli ping` 确认 Redis 活着；看 `tail -20 celery.log` |
| 处理报错 | 任务页有完整日志；`tail -50 server.log` 看 HTTP 层错误 |
| AttributeError: module 'numpy' has no attribute 'in1d' | numpy 2.x 移除了 np.in1d/np.int_。stdpipe (local-fixes 分支) 与 stdweb (local-zh 分支) 均已改为 np.isin/int()。改代码后**必须重启 celery**（长驻进程不会自动加载新代码） |
| 盲解算失败 | 检查 /etc/astrometry.cfg 的 add_path 是否指向 index 目录（cpulimit 300 是 5 分钟上限） |
| 模板相减下载模板失败 | 需要外网；检查 `tail -20 celery.log` 中 PS1/SkyMapper 下载报错 |
| 改 .env 后不生效 | 重启服务：`stop_stdweb.sh && start_stdweb.sh` |
