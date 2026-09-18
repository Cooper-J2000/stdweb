# STDWeb 本地部署说明（local-zh 分支）

本文档记录本机 STDWeb 部署的全部本地化改动、日常运维、更新流程与已知问题。
代码位于 /home/ajst/Astro_Software/stdweb，所有本地改动提交在 `local-zh` 分支
（master 保持与上游 https://github.com/karpov-sv/stdweb 一致）。

> 交接提示：接手本机 STDWeb 运维，请先通读本文档；界面操作以本地化改动清单为准，
> 与上游英文 README.md 的描述有出入时以本文档为准。

## 目录

- [部署架构](#部署架构)
- [开机自启动](#开机自启动)
- [本地化改动（相对上游，共 12 项）](#本地化改动相对上游共-12-项)
- [日常使用](#日常使用)
- [更新上游代码](#更新上游代码)
- [环境重装](#环境重装)
- [日志文件](#日志文件)
- [故障排查](#故障排查)
- [已知硬件问题](#已知硬件问题)

## 部署架构

| 组件 | 说明 | 管理方式 |
|---|---|---|
| Redis | 任务队列 broker（系统服务，仅 127.0.0.1:6379） | `sudo systemctl start/stop redis-server` |
| Celery worker | 后台处理，6 并发（12 个 astropy worker 对 15GB 内存偏多） | systemd 用户服务 `stdweb-celery.service`（开机自启），或 `start_stdweb.sh` / `stop_stdweb.sh` |
| Django runserver | Web 服务，仅监听 127.0.0.1:27102 | systemd 用户服务 `stdweb-django.service`（开机自启），或 `start_stdweb.sh` / `stop_stdweb.sh` |
| conda 环境 | `stdweb`（Python 3.12.13） | 见下文 |

Python 环境：`/home/ajst/miniconda3/envs/stdweb/bin/python`
（**不要**用 `conda run`——本机 conda 26.3.2 的 activate 钩子有 bug；建/改环境必须加
`--solver classic`，详见[环境重装](#环境重装)）。

关键路径：

- 代码：`/home/ajst/Astro_Software/stdweb/`
- STDPipe（editable 安装，local-fixes 分支）：`/home/ajst/Astro_Software/stdpipe/`
- 上传数据（公共文件区）：`stdweb/data/`（.gitignore）
- 任务文件：`stdweb/tasks/`（.gitignore）
- 巡天模板缓存：`stdweb/ps1_cache/`（.gitignore，可界面一键清理，见本地化改动第 8 项）
- 配置：`stdweb/.env`（.gitignore，含 SECRET_KEY、二进制路径、STDPIPE_PS1_CACHE、AJST_TOKEN）
- 数据库：`stdweb/db.sqlite3`（.gitignore）
- 日志：`celery.log`、`server.log`（runserver 文件日志）、`django.log`（异常日志，见日志章节）

## 开机自启动

Celery 和 Django 由 **systemd 用户级服务**托管，开机自动拉起（无需登录桌面，
用户 `ajst` 已开启 linger；Redis 本身已是 enabled 的系统服务）：

- 单元文件：`~/.config/systemd/user/stdweb-celery.service`、`~/.config/systemd/user/stdweb-django.service`
  （日志仍分别写入项目根目录 `celery.log` / `server.log`，与原 nohup 方式一致）
- celery 另有 drop-in `~/.config/systemd/user/stdweb-celery.service.d/override.conf`：
  注入 `LD_LIBRARY_PATH=/home/ajst/Astro_Software/cfitsio_latest/lib`，否则 systemd 干净环境下
  HOTPANTS 找不到 `libcfitsio.so.10`（库不在系统路径，只在 ~/.bashrc 里 export），
  图像相减会静默失败（日志只有 "HOTPANTS run failed" 警告，任务状态照样显示 subtraction_done）。
- 常用命令：
  ```bash
  systemctl --user status stdweb-celery stdweb-django   # 查看状态
  systemctl --user restart stdweb-celery                # 改了 .py 后重启 celery
  systemctl --user stop/start stdweb-django             # 手动停/启
  systemctl --user disable stdweb-celery stdweb-django  # 取消开机自启
  ```
- 崩溃自动重启：`Restart=on-failure`（对本机偶发 SIGSEGV 的硬件问题有自愈效果）。
- `start_stdweb.sh` / `stop_stdweb.sh` 仍可用：systemd 托管的进程能被脚本 pgrep 识别，
  且 SIGTERM 退出码属"正常退出"不会触发 systemd 自动重启，两种方式不冲突。

## 本地化改动（相对上游，共 12 项）

全部提交在 `local-zh` 分支，master 保持与上游一致。`git log --oneline local-zh` 可查完整历史。

1. **全界面中文汉化**：21 个模板、forms.py（90 个字段标签）、views.py / views_tasks.py
   （58 条提示消息）、filters.py（新增 `state_cn` 状态过滤器）。天文学专有名词保留英文。
2. **安全收紧**：`stdweb/settings.py` 的 `ALLOWED_HOSTS` 改为从环境变量读取，
   默认 `127.0.0.1,localhost`（可在 .env 用 `ALLOWED_HOSTS` 覆盖）。**不做任何外部暴露**。
3. **Files 删除功能**：文件浏览页每个文件有删除按钮（`views.py` 的 `list_files` POST 处理 +
   `files.html`）。安全设计：软链只删链接本身（不碰目标文件）、普通文件有路径穿越防护（realpath
   检查）、仅登录可用。（2026-09-04 扩展）新增**批量勾选删除**：每行末尾新增删除勾选框
   （所有非目录文件可选，区别于仅 FITS 的"待处理"勾选框），表头"删除所选"按钮按勾选数
   动态启用，POST `action=batch_delete` + `filenames` 列表，逐文件复用同一套删除安全检查
   （抽出为 `views.py` 的 `try_delete_file`）；删除后重定向保留当前排序参数。
   同次新增**列表排序**：表头"文件名"/"时间"两列各有 ↑↓ 链接（GET `sort=name|time` +
   `order=asc|desc`，默认文件名升序，非法参数回退默认），每行显示修改时间；任务文件
   浏览页复用同一视图，同样支持排序但不显示任何删除 UI。
4. **首页上传选择 FITS 扩展层 (HDU)**：上传表单新增"FITS 扩展层 (HDU)"下拉
   （默认"自动（最后一个 HDU）"，可显式选 HDU 0-10）。`forms.py` 的 `UploadFileForm`
   新增 `ext` 字段；`views.py` 的 `upload_file` 按选择用 `fits.getdata/getheader`
   提取该扩展为单扩展 image.fits（保留其头信息）。
   注：处理端统一读 `fits.getdata(image.fits, -1)`（最后一个 HDU），多扩展 FITS
   若数据不在最后扩展，务必用本功能或 Files 导入（详情页"处理此扩展名"）选定正确扩展。
5. **Files 页面上传（支持多选）**：文件列表页顶部"上传到数据区"表单，可一次选多个文件
   （`views.py` 的 `upload_data` 视图 + `urls.py` 注册 + `files.html` `multiple` 属性）。
   文件存入 DATA_PATH 但不创建任务；同名文件跳过并列出；文件名经 `os.path.basename` 净化防穿越。
6. **首页缓存管理面板**：上传表单下方"缓存管理"卡片（`views.py` 的 `get_cache_entries` +
   `clear_cache` + `index.html`）。按巡天逐项列出缓存目录（ps1/ls/ls11/lsdr11 及各 HiPS
   巡天，均为 ps1_cache/ 下的子目录）及 astropy 下载缓存、astroquery 查询缓存，
   每项显示当前大小并可单独清除（POST `key` 白名单校验）。登录保护。
   **注意**：首页 '/' 实际由 `upload_file` 视图渲染（不是 index 视图），cache_entries 需两处提供。
7. **Bug 修复：Files 导入任务 500（ext=auto）**：`UploadFileForm` 新增 ext 字段后，
   Files 详情页表单默认带 `ext=auto`，但 `upload_file` 的 local_files 分支只判断 `ext is None`，
   `'auto'` 会走 `int('auto'[0])` 抛 ValueError → 500（任务创建但 image.fits 未复制）。
   修复：`ext is None or ext == 'auto'` 时整文件复制；forms.py ext choices 补 HDU 0 (PRIMARY)。
8. **numpy 2.x 兼容修复**：numpy 2.0 移除了 `np.in1d` 和 `np.int_`。stdweb 侧
   `processing/transients.py`、`processing/catalogs.py` 已改 `np.isin`；stdpipe 侧
   `pipeline.py`（6 处）和 `photometry.py`（1 处）在 stdpipe 的 `local-fixes` 分支已改。
   **改 stdpipe/stdweb 的 .py 后必须重启 celery**（长驻进程不会自动加载新代码）。
9. **运维脚本**：`start_stdweb.sh` / `stop_stdweb.sh`（celery 并发固定 6；
   stop 脚本在 pkill 后等待进程完全退出（runserver 10s / celery 20s 超时强制 kill），
   修复了"stop 后立即 start 时旧 celery 未退净导致误判已在运行"的竞态）。
10. **上传测光结果至 AJST 星表（AJST_Transient_lc_Cata）**：新增 `views_ajst.py` +
    `templates/ajst.html` + `AJSTSelectForm`（forms.py）+ 路由 `/ajst/` +
    权限 `stdweb.ajst_upload`（migration 0017，仅 Meta 变更）。
    入口：导航栏 + 任务页直接测光/模板相减两个区块的按钮（门控
    `perms.stdweb.ajst_upload and settings.AJST_TOKEN`）。
    三步流程：选择（任务 ID 范围 + direct/subtracted 复选）→ 卡片式可编辑预览
    （所有字段可改，按磁盘实际存在的 target.vot/sub_target.vot 逐行出卡片、逐行勾选）
    → 上传（以表单提交值为准，不重读 .vot）。
    对端是 AJST 星表的 ingest API（Bearer token，见该项目的 技术文档.md §8.14）。
    配置：`.env` 的 `AJST_BASE_URL` / `AJST_TOKEN`（另见 doc/configuration.rst 与 README.md）。
    **完整设计与实施档案见 `doc/ajst_upload_design.md`**（含接口契约、去重规则、修订记录）。
    顺带修复：`skyportal()` 视图补上缺失的 `@permission_required('stdweb.skyportal_upload')`
    （此前仅靠隐藏入口"禁用"，登录用户直接 POST 即可用）。
11. **默认参数与文件头关键字调整**（2026-08-09）：
    - "初始检查与掩模"的"宇宙线掩模"默认**不勾选**（forms.py `mask_cosmics` initial=False；
      inspect.py 处理端 `config.get('mask_cosmics', ...)` 默认同步改 False）；
    - 文件头解析新增两个关键字：时间 `DATE-MID`（曝光中值时刻，优先级高于 DATE-OBS，
      在 stdpipe `utils.py` 的 `get_obs_time`，local-fixes 分支）；滤光片 `FAFLTNM`
      （inspect.py 的关键字遍历列表，排在 FILTER/FILTERS/CAMFILT 之后）；
    - "测光与天体测量"的"相对孔径, FWHM"默认 1.5（forms.py initial + inspect.py 默认同步）；
    - "模板相减"的"HOTPANTS 附加参数"默认 `{"ko": 2, "bgo": 2}`（forms.py initial +
      inspect.py 默认同步）。
    - 注意：新默认值只对**新任务**生效；旧任务 config 已存的值会以 `initial=task.config` 覆盖显示。
12. **新增星表与模板支持**（2026-08-15）：
    - "测光与天体测量"参考星表新增 **Pan-STARRS DR2**（`ps1dr2`）和 **Legacy Survey DR11**
      （`lsdr11`）；"模板相减"模板新增 **Legacy Survey DR11**（`ls11`）。
    - stdweb 侧：`processing/constants.py` 的 `supported_catalogs`/`supported_templates` 各加条目
      （表单下拉自动生效）；`processing/photometry.py` 对 `lsdr11` 改调
      `stdpipe.catalogs.get_cat_lsdr11()`；`processing/subtraction.py` 模板分支纳入 `ls11`
      （maskbits 位定义 DR11 与 DR10 相同），且 `ls11` 模板缓存用 `ps1_cache/ls11/` 子目录
      （DR11 砖文件名与 DR10 相同，防止串缓存）。
    - stdpipe 侧（local-fixes 分支）：`catalogs.py` 加 `ps1dr2`（指向 Vizier `II/389/ps1_dr2`；
      注意 `II/349` 是 DR1、`II/389` 才是 DR2，原 `ps1` 条目未动）和 `get_cat_lsdr11()`（从 NERSC 逐砖下载
      `tractor-<brick>.fits`，流量转星等 `mag=22.5-2.5log10(flux)`，复用 PS1 换算增广 BVRI）；
      `templates.py` 的 `find_skycells`/`get_skycells` 支持 `survey='ls11'`（dr11/south、
      dr11/north coadd）；新增数据文件 `stdpipe/data/legacysurvey_dr11_bricks.fits.gz`
      （由 `data/legacysurvey_dr11.py` 一次性生成）。
    - 注意：DR11 north（BASS/MzLS）无 i 波段（其 tractor 砖表**完全没有 flux_i 列**，
      读取时自动补零 → imag 为 NaN，2026-08-15 修复此导致的 KeyError）；砖选择除球面粗选外
      再按 0.25°×0.25° 砖盒与视场圆的实际重叠过滤，避免下载不搭界的角部砖；
      LS 星表流量已做银河消光修正，与 PS1 等未消光星表混合时定零可能有微小系统差；
      `lsdr11` 星表按砖缓存于 `STDPIPE_PS1_CACHE/lsdr11/`（首页缓存管理面板可单独清理）。
13. **缓存机制改造**（2026-08-18）：
    - **按巡天分目录**：`STDPIPE_PS1_CACHE` 仍为根目录，其下每个巡天一个子目录
      （`ps1/`、`ls/`、`ls11/`、`lsdr11/` 星表，以及 HiPS 巡天 `skymapper/`、`des/`、
      `decaps/`、`ztf/`、`2mass/`）。`processing/subtraction.py` 按模板名拼接子目录。
      原根目录下平铺的 PS1 skycell 已迁入 `ps1/`。
    - **HiPS 模板本地缓存**（stdpipe `templates.py`）：`get_hips_image` 新增 `_cachedir`
      参数。请求被规范化为网格对齐、加边填充的 tile（中心按半视场间距对齐天区网格、
      尺寸补足，文件名含巡天+尺寸+量化中心+CD矩阵哈希），缓存命中后经
      `reproject_lanczos` 重投影到任务精确网格；asinh 线性化在存盘前完成。
      upscale 请求不走缓存。
    - **flock 进程间锁**（stdpipe `utils.file_lock`）：skycell、HiPS tile、LS DR11
      星表砖三处下载均先抢独占锁并锁内二次检查，杜绝多 worker 并发重复下载；
      锁文件遗留（空文件无害），崩溃自动释放。
    - **astropy 下载缓存不再增长**：`fits_open_remote` 默认 `cache=False`
      （管线有自己的文件缓存，astropy 的 URL 缓存永不过期只会膨胀）；
      HiPS 下载同样 `cache=False`。已有的 2.4GB astropy 下载缓存已清空。
14. **滤光片别名本地化**（2026-08-18）：`processing/constants.py` 的 `supported_filters`
    新增别名——`up`→Sloan u、`gp`/`rp`/`ip`→Pan-STARRS g/r/i、`w`→Gaia G。
    inspect 步骤从 FITS 头读到这些值时自动归一化，测光定标随之选用对应星表波段
    （u 可用 gaiadr3syn/sdss；G 可用 gaiaedr3）。（2026-09-04 补充）再增
    `U_Sloan`→Sloan u、`G_Sloan`→Pan-STARRS g、`R_Sloan`→Pan-STARRS r、
    `I_Sloan`→Pan-STARRS i、`Z_Sloan`→Pan-STARRS z。

其他小改动：`settings.py` 增加 LOGGING 配置（django.log 异常日志，见日志章节）。

## 日常使用

```bash
# 启动（Redis + Celery + Django，仅本机可访问）
/home/ajst/Astro_Software/stdweb/start_stdweb.sh

# 停止（Redis 保持运行）
/home/ajst/Astro_Software/stdweb/stop_stdweb.sh

# 访问
# 浏览器打开 http://127.0.0.1:27102
```

登录凭据：admin 账号（密码已由使用者修改；若遗忘用下面命令重置）。
改密码：

```bash
cd /home/ajst/Astro_Software/stdweb
/home/ajst/miniconda3/envs/stdweb/bin/python manage.py changepassword admin
```

多扩展 FITS 推荐流程（数据不在第一个 HDU 时）：

1. Files 页 → "上传到数据区"（可多选；或直接 `cp` 到 data/ 目录）
2. Files 页点开该文件 → 预览各 HDU（PRIMARY/SCI/WEIGHT…）→ 确认科学数据所在扩展
3. 点该扩展的"处理此扩展名"导入任务；或勾选多个文件"处理所选文件"批量导入
4. 主页直接上传时，用"FITS 扩展层 (HDU)"下拉选定扩展（默认自动 = 最后一个 HDU）

模板缓存管理：首页"清理模板缓存"按钮一键清理 ps1_cache/（释放磁盘，重处理时自动重下）。

### 启动脚本说明

`start_stdweb.sh` 依次检查/启动：Redis（systemd）→ Celery worker → Django runserver，
就绪后输出访问地址。脚本用 `pgrep -f "[p]ython.*-m celery"` 精确匹配（[x] 技巧防自匹配）。
**注意**：在 Hermes agent 环境里 nohup 启动的后台进程可能被清理，服务托管用 Hermes 的
background 进程；用户手动在真实终端跑 start_stdweb.sh 则无此问题。

## 更新上游代码

**必须全程在 local-zh 分支操作**（本地化改动都在这个分支）：

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
- 上游若改了 settings.py 且 ALLOWED_HOSTS 冲突：重新应用本地收紧（见本地化改动第 2 项）
- **上游若改了模板/表单/视图**：重新应用汉化和功能 patch（对照[本地化改动](#本地化改动相对上游共-9-项)清单逐项检查）
- 模板/代码改动由 runserver autoreload 自动生效；celery 需重启才加载新代码

stdpipe 仓库同样维护在 `local-fixes` 分支（numpy 2.x 修复），更新方式相同：
`git checkout master && git pull && git checkout local-fixes && git merge master`。

## 环境重装

```bash
# conda 插件有 bug，必须加 --solver classic
conda create -n stdweb python=3.12 pip -y --solver classic
# 先 pin Django <6（DRF 与 Django 6 不兼容），再装依赖
/home/ajst/miniconda3/envs/stdweb/bin/pip install "Django>=5.2,<6"
/home/ajst/miniconda3/envs/stdweb/bin/pip install -e /home/ajst/Astro_Software/stdpipe
/home/ajst/miniconda3/envs/stdweb/bin/pip install -r requirements.txt
```

外部二进制依赖（已装好）：SExtractor(/usr/local/bin/sex)、SCAMP、HOTPANTS
(/home/ajst/Astro_Software/hotpants/hotpants)、Astrometry.Net solve-field + 66GB index
(/home/ajst/Astro_Catalog/index_for_Astrometry/，配置 /etc/astrometry.cfg)。

数据库迁移与管理员：

```bash
cd /home/ajst/Astro_Software/stdweb
/home/ajst/miniconda3/envs/stdweb/bin/python manage.py migrate
/home/ajst/miniconda3/envs/stdweb/bin/python manage.py createsuperuser
```

## 日志文件

| 文件 | 内容 | 位置 |
|---|---|---|
| celery.log | celery worker 输出（任务处理日志、错误堆栈） | stdweb/ 根目录 |
| server.log | runserver 的 HTTP 请求日志 | stdweb/ 根目录 |
| django.log | django.request 异常 traceback（500 排查用，settings.py LOGGING 配置） | stdweb/ 根目录 |
| 任务页日志 | 每个任务目录下的 *.log（inspect.log、photometry.log、transients_simple.log 等） | stdweb/tasks/<id>/ |

注：runserver 由 Hermes 托管时输出在 Hermes 进程日志而非 server.log；
django.log 是本地诊断日志（untracked），可随时删除。

## 故障排查

| 症状 | 处理 |
|---|---|
| 页面没样式/JS | runserver 必须带 `--insecure`（DEBUG=False 时不服务静态文件），start 脚本已带 |
| celery 进程假死 | `stop_stdweb.sh && start_stdweb.sh`（stop 已带等待退出，不会误判） |
| 任务一直排队不跑 | `redis-cli ping` 确认 Redis 活着；看 `tail -20 celery.log`；确认 celery 进程存在（`pgrep -af celery`） |
| 处理报错 | 任务页有完整日志；`tail -50 server.log` 看 HTTP 层错误；`tail -30 django.log` 看 500 traceback |
| AttributeError: module 'numpy' has no attribute 'in1d' | numpy 2.x 移除 np.in1d/np.int_，stdpipe/stdweb 均已修复。若再现：确认 stdpipe 在 local-fixes 分支且 **重启 celery** |
| _compression.CfitsioException: unused bytes at end of compressed buffer | astropy 下载缓存(~/.astropy/cache/download/url/*/contents)中某 skycell 文件损坏（常见于下载时系统不稳定）。删除坏缓存文件后重跑即恢复。批量定位: `python -c "from astropy.io import fits; fits.open('<缓存文件>')[1].data"` |
| Files 导入任务报错/任务目录空 | 检查是否复现 ext=auto 500 bug（本地化改动第 7 项已修）；任务目录空说明导入时 image.fits 未复制，删除空壳任务重新导入 |
| 盲解算失败 | 检查 /etc/astrometry.cfg 的 add_path 是否指向 index 目录（cpulimit 300 是 5 分钟上限） |
| 模板相减下载模板失败 | 需要外网；PS1 模板检查 `tail -20 celery.log` 下载报错；LS(Legacy Survey) 模板在 DR9 north 部分天区（如 RA 11-12 附近）NERSC coadd 路径无数据，北天目标建议直接用 PS1 |
| 模板相减"成功"但无差分产物 | HOTPANTS 报 `libcfitsio.so.10: cannot open shared object file`：celery 的 systemd 环境缺 `LD_LIBRARY_PATH`。已由 drop-in `stdweb-celery.service.d/override.conf` 修复（见[开机自启动](#开机自启动)）；注意相减失败只记 warning，任务状态仍显示 subtraction_done，需看 celery.log 确认 |
| 改 .env 后不生效 | 重启服务：`stop_stdweb.sh && start_stdweb.sh` |
| python 进程段错误 (SIGSEGV) | 见[已知硬件问题](#已知硬件问题) |

## 已知硬件问题

**本机存在疑似硬件不稳定的症状，排查应用层原因均排除，属系统级问题：**

- 现象：python 进程偶发 SIGSEGV（段错误，退出码 139），崩溃位置在 CPython 解释器自身代码段；
  内核日志（dmesg）显示集中在 CPU 4/5（core 8，E 核）；2026-08-07 曾出现 16 次内核 Oops
  （ext4_file_open page fault），重启后 oops 消失但用户态 segfault 仍偶发。
- 触发特征：**服务进程（celery/runserver）运行时**高负载下更容易出现；服务停止后明显减少。
- 建议排查顺序：① 备份重要数据；② memtest86+ 内存测试（1-2 轮）；③ `stress-ng --cpu 12`
  压力测试观察报错；④ 若确认硬件：BIOS 关闭 E 核/超线程临时缓解，或更换内存条。
- 运维提示：遇到 python 进程 segfault 时**重试即可**（偶发），不影响数据完整性；
  若频繁出现先重启系统，再考虑硬件诊断。
