# 项目代理约束

## 迁移模块

- 版本迁移必须由独立的 `bilibili_podcast.config.migration` 模块负责；业务服务、Web、同步器和部署脚本不得各自实现迁移逻辑。
- 迁移目标始终是当前代码定义的最新版本。每个已经发布的历史版本都必须存在可检测的来源版本和连续升级路径，禁止只支持“上一版本 → 当前版本”。
- 新增或修改配置、SQLite schema、文件布局、unit、Cookie 或发布格式时，必须同步新增迁移步骤和对应历史版本 fixture。
- 持久状态相关的新功能与迁移步骤、历史 fixture 必须在同一个 feature 中完成并共同验证，不得延期补充。
- 历史迁移步骤一经发布不得改写语义；修复必须新增后续步骤，保证老安装仍可按顺序升级。
- 未标记的早期安装必须通过显式的 `legacy-unversioned` 适配器进入版本链。未知、未来版本、损坏状态或缺失迁移步骤必须显式失败。
- 最早生产布局必须通过独立 `legacy-v0` profile 和权限受限的 layout manifest 进入版本链；fixture 必须保持真实 partial env 形态，禁止用人为补全的统一环境变量替代历史输入。
- 默认执行 dry-run。实际写入前必须完成在线备份、checksum、staged 验证和回滚准备；失败不得留下半迁移状态。
- 活动 SQLite 只能原位执行向后兼容事务迁移；禁止替换 inode。apply 必须独占统一配置中的 sync lock，配置读取通过 migration lock 共享锁保持快照一致。unit、timer、crontab、用户和组等系统状态必须列入计划，并通过单独授权的系统迁移步骤执行。
- 迁移测试必须覆盖：最老 fixture、每个中间版本、当前版本幂等、跳过多个版本、未知未来版本、损坏状态、锁冲突与失败回滚。
- 历史 fixture 与迁移步骤必须动态覆盖 `EARLIEST_UNIFIED_VERSION..LATEST_VERSION` 的连续区间；提升最新版本但未同时加入 snapshot/step 时，测试必须失败。
- 零停机候选版本必须使用 immutable release 和独立 venv；禁止在活动代码目录执行 `git pull` 或覆盖活动 venv。release 准备、symlink 激活、服务 reload/restart 必须是独立门禁。
- 两个鉴权 upstream 必须运行同一候选版本并共享唯一配置源；Web 监听覆盖只能是显式的一次性 CLI 参数，不得新增持久环境变量。
- 标准化脚本为候选 Web 主备 unit 配置端口时，两个端口必须成对提供且不同；脚本只写入和备份 unit，不得隐式调用 `systemctl`。

## B 站 API 后端

- 测试必须全部 mock，禁止在测试用例中发起真实 B 站网络请求；真实请求只允许出现在低频、单条、只读的手动冒烟验证中。
- 手动冒烟验证只允许使用无风控风险（或低风险）的接口：`x/web-interface/view`（视频信息）、`pgc/view/web/season`（剧集）；禁止用风控严格接口做验证：`x/web-interface/search/type`（搜索）、`x/space/wbi/arc/search`（空间视频列表）、`x/polymer/web-space/seasons_series`（空间系列列表）。
- 真实冒烟请求必须：匿名优先（不携带生产 cookie）、请求间隔 ≥ 3 秒、单条执行；响应非 JSON 或触发风控（-352/-799/-412/403）时立即停止，等待冷却后再继续，不得自动重试或加速。
- 新增 B 站接口适配时，先以 mock 完成字段映射与错误码测试；真实字段验证需要时使用上述低风险接口，并在验证记录中注明接口名、日期与结果。
- 验证脚本与测试不得留存真实 cookie、token 或凭证；示例一律使用占位符。

## 生产迁移安全

- 生产探针必须携带完整正确参数，保持单条、低频、分阶段；禁止对受 fail2ban 保护的入口批量执行错误 token 或缺参测试。
- commit、push、远端写入、Nginx/systemd reload、权限调整、公网 IP 变更和删除仍需分别取得明确授权。
