# AStockTail 涨幅榜尾盘观察程序 v1.0

这是一套独立的每日观察程序：通过 eltdx 涨幅榜有限扫描建立预选池，最多对100只股票做分钟技术分析，保留三组对照，并补录后续收益。不下单。

它不改写原来的 strategy.py、config.json、app.py 和 replay_reports/five_days/cache；也不自动关闭原有任务。原来的网页不会自动显示新结果。本版不扩大历史回测。

## 文件

- astocktail_observer.py：接口检查、收盘后演练、正式每日观察、收益补录。
- test_observer.py：32项离线自动测试，模拟数据和请求，不会发送消息。
- install_observer.sh：Ubuntu systemd 定时器安装脚本。
- README_observer.md：本说明。

依赖：Linux、Python 3.11或更新版本、你现有虚拟环境中的 eltdx。无需AKShare或pandas。不强制升级现有依赖；先用doctor检查已安装版本。

## 先在腾讯云检查和演练

把压缩包提交到 GitHub 仓库根目录，随后在腾讯云运行。每条成功后再运行下一条；报错时保留输出。

```bash
cd ~/astocktail-cloud
git pull --ff-only origin main
.venv/bin/python -m zipfile -e astocktail_observer_v1.zip .
.venv/bin/python -m unittest -v test_observer.py
.venv/bin/python -u astocktail_observer.py doctor
.venv/bin/python -u astocktail_observer.py rehearse
```

也可以直接上传压缩包解压后的四个文件，跳过解压命令。

`doctor`检查已安装eltdx版本、涨幅榜排序、必要行情字段、流通股本和基准指数交易日期，最多约45秒网络预算，不发送消息。

`rehearse`在当前服务端榜单上做一次完整预选和技术复核，不等待三个盘中时点，不推送，不计入正式信号或后续统计。只允许最新交易日已收盘（北京时间15:10以后），或当前是该交易日之后的日期。最多约5分钟网络预算。演练仅说明当时接口及处理链路可运行，不代表14:50性能已经验证。其资料是演练时取得，不能冒充历史14:49完整回测。

## 开启自动运行

先确认doctor通过且演练结果能读取。通知使用原先的环境变量名：`SERVERCHAN_SENDKEY`。

如果已有含这个键的 `.env` 文件，可直接复用：

```bash
sudo bash install_observer.sh "$PWD" ubuntu "$PWD/.env"
```

如果密钥在别处，先在项目目录新建 `observer.env`，只填写：

```text
SERVERCHAN_SENDKEY=你的现有密钥
```

然后：

```bash
chmod 600 observer.env
sudo bash install_observer.sh "$PWD" ubuntu "$PWD/observer.env"
```

密钥文件不要提交GitHub。安装器把默认 `observer.env` 和 `observer_data/` 加入本地 Git 排除；自选其他文件名时需自行排除。如果文件已被Git跟踪，排除规则不能把它自动移出版本历史。

安装器会安装并启用两个系统级定时器，只启动定时器，不立即运行选股或发消息。没有密钥也能运行，但通知侧记录 `missing_SERVERCHAN_SENDKEY`。关闭腾讯云网页或SSH连接不影响systemd任务；服务器本身必须保持运行。

原先的 GitHub Actions、cron、systemd任务没有被修改。若旧任务也会推送，请停用对应旧任务，避免两套程序重复发送；不要误停其他服务器服务。

## 固定日程

所有时间均为北京时间，韩国时间加1小时。

| 时间 | 工作 |
|---|---|
| 14:39:30 | systemd启动，检查当天是否已有基准指数日K |
| 14:40、14:45、14:49 | 三次涨幅榜预选、准备股本和历史日K |
| 14:50 | 对最多100只候选获取完整分钟线，使用截至14:49的数据 |
| 14:54:30 | 不再发起新的最终行情请求，保存已完成结果并准备通知 |
| 14:55 | 不再发送带候选的尾盘通知；超过此点只发送状态 |
| 15:20、16:20 | 补录次日、3日、5日行情表现，更新累计和周度CSV |

定时器按周一至周五触发，程序用基准指数日K确认当前交易日。节假日或日K尚未更新时，记为 `no_current_session_data`，不生成候选；不把无法确认行情简单宣布为“正常休市”。周末不扫描。不补执行错过的尾盘任务。

手动正式运行只允许14:35至14:50前启动：

```bash
.venv/bin/python -u astocktail_observer.py run --notify --env-file observer.env
```

通常交给定时器即可。当天已有 signals.json 时直接退出，不重算、不重复通知。

## 有限扫描与效率限制

1. eltdx的 `quotes.list_by_category("沪深A股", sort_by="涨幅", ...)` 按涨幅降序，每页80只，最多20页或15秒，先达到哪个就停止。
2. 寻找2.5%—5.5%的预选区间；完整有序页降到2.5%以下后结束。不能只取前100名，因为这些股票可能全涨了5%以上。
3. 候选准备最多保留300只股票的日K；股本和日K在本次运行内复用。预选暂用量比>0.8、换手3%—12%、流通市值45亿—220亿元，给最终条件留余量。
4. 预选池按预选量比降序、换手率降序、代码升序排序，最多100只进入分钟复核。早先入池、后续榜单没再出现的股票可保留，最终重新核验，不将旧预选数值当作最终值。
5. 最终推送主要观察组最多3只，按最终量比降序、换手率降序、代码升序排序。全体三组信号另行保存。
6. 接口错误、页数上限、排序异常、榜单翻页变动、资料缺失、容量跳过、最终数据失败和超时均进入报告。允许漏选是设计约束，`ok`只说明限定范围内的运行状态，不代表全市场完整覆盖。

100只技术分析的网络开销有之前抽样测速支持，但尚未实测新榜单完整链路。单次网络请求使用独立子进程硬超时，避免底层请求无限挂住。最多补试一次缺失的最终数据，不无限重试。尾盘接口变慢时减少覆盖，不放宽条件凑数量。

## 保留的三组规则

| 组 | 条件 |
|---|---|
| C | 原始C形态与共同基础/技术条件 |
| C_MA5_GT_MA10 | C并且MA5>MA10，主要观察/推送组 |
| B_MA5_GT_MA10 | B并且MA5>MA10，严格对照组 |

最终基础条件保持：涨幅3%—5%、量比>1、换手5%—10%、流通市值50亿—200亿元。量比定义沿用V3：当天截至14:49的每分钟均量÷此前5个完整交易日的每分钟均量，不能默认等于其他行情软件的“量比”字段。

共同技术条件沿用现有ABC程序：10:00—14:49的200个交易分钟分成五组，每组40分钟；第五组成交量中位数大于第三、第四组中位数的平均值；分时涨幅强于对应指数；至少70%的分钟收盘价位于累计VWAP上方。

B：14:40—14:49内高点突破14:00以来的此前高点。

C：14:40—14:49内高点达到13:01以来此前高点的99.5%以上，包括突破。

回踩必须发生在触发后的完整分钟，深度至少0.05%；从触发后的第一分钟到该回踩分钟之间的低点全部不低于对应VWAP。保持现有ABC实现，不额外加“回踩之后直到14:49都不能破VWAP”的新条件。

均线以信号日14:49价格替代当日收盘，其余使用此前完整日K。沿用趋势程序的20个历史完整日K可用性门槛；不足时MA组不通过数据验证，原C仍可独立记录。

数据质量检查更明确：个股和指数均须覆盖09:31—11:30、13:01—14:49全部229根完整分钟线；重复、缺失、非有限数值或旧日期拒绝。此前5日成交量必须与市场日历匹配。日K昨收和榜单昨收不一致时先排除并记原因，避免把明显口径冲突用于信号。以上属于缺失/异常处理，不把它们算作条件正常不通过。

当前披露股本可能滞后于真实股本变化，保留取得时间和提供者更新日期，结果为观察候选，不宣称交易所严格核验。

## 存档与收益

输出全部位于 `observer_data/`：

- `doctor/<运行时间>/doctor.json`：接口检查报告。
- `rehearsal/<运行时间>/signals.json`：演练结果，与正式样本隔离。
- `live/YYYY-MM-DD/signals.json`：当天不可覆盖的信号、三组标记、条件通过数、跳过数量和版本哈希。
- `live/YYYY-MM-DD/prepared_inputs.json`：本次实际使用的资料及日K，供复核。
- `live/YYYY-MM-DD/raw/`：每次请求的输入、取得时间、耗时及返回行情。
- `live/YYYY-MM-DD/notification_*.json`：通知状态。`accepted_by_service`只表示服务端受理，不证明手机已显示。
- `outcomes/latest.json`：后续结果。
- `outcomes/history/`：历次更新快照。
- `outcomes/summary.csv`：累计和ISO自然周的三组统计。

`signals.json`中的 `pushed` 是计划推送名单；实际是否被服务受理需结合notification文件。汇总中的 `planned_push` 也是这份计划名单，不假定通知成功或实际买入。

收益按信号日14:49价格为基准，统计随后第1、3、5个市场交易日收盘收益，以及对应观察期内的最高/最低价相对信号价的变化。三组全体入选均被跟踪，缺少后续数据或尚未到期留空；股票停牌/缺失某交易日时，不跳到下一根K线冒充原目标日。已取得的各期结果保留，不随重跑悄悄修订。

这些是未复权、未扣费用和滑点的价格表现，不是实盘收益。除权除息需另外核查，最低价相对入场参考价不等同于净值最大回撤。程序不模拟“14:49实际买入”，不同时把次日最高、最低当作可成交价格。

每次收盘更新都生成周度表，不额外发送周报。已经冻结的信号不参与后续调参重算。

## 查看、停止

```bash
systemctl list-timers --all 'astocktail-observer-*'
journalctl -u astocktail-observer-run.service -n 100 --no-pager
journalctl -u astocktail-observer-update.service -n 100 --no-pager
cat observer_data/outcomes/summary.csv
```

```bash
sudo systemctl disable --now astocktail-observer-run.timer astocktail-observer-update.timer
```

关闭timer不会中断已经启动的service，如要立即停止本次运行，再执行：

```bash
sudo systemctl stop astocktail-observer-run.service astocktail-observer-update.service
```

停止进行中的运行可能留下原始数据和progress，但不会凭空生成完整信号。

## 已完成验证与限制

32项自动测试通过，覆盖分钟缺失/重复/旧数据、禁止未来分钟及当天收盘参与信号、原基础与三组判断、100只上限、涨幅榜跨页、排序异常、硬超时、信号不可覆盖、正式运行与收盘更新隔离、通知状态文本及1/3/5日市场日历计算。

另用80组生成分钟数据比对原ABC的共同技术条件和B/C形态，结果一致。安装脚本通过bash语法检查，定时表达式另行检查。

尚未在你的腾讯云执行本程序，也未测试真实榜单响应、盘中完整耗时和通知实际送达；前述测试使用模拟数据或原函数对照，不能替代目标环境验证。

接口依据：
https://github.com/electkismet/eltdx/blob/main/docs/METHOD_REFERENCE.md
https://github.com/electkismet/eltdx/blob/main/docs/helpers/股票信息汇总.md
