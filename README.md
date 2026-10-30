# AStockTail Cloud — 免费沪深A股尾盘筛选网页

GitHub Actions 负责北京时间交易日14:42启动（**可能延迟/丢失**）、Python/AKShare筛选、JSON归档、可选手机通知；Streamlit Community Cloud负责展示GitHub仓库中的记录。**不是有交易所授权时间戳的实时行情系统，不保证14:50准时输出，也不交易股票。**

## 1. 部署（不需要本地Mac一直开机）

1. 注册/登录 [GitHub](https://github.com)，点击 **New repository**，名称 `astocktail-cloud`，建议先建 **Public**。不要上传密钥/手机号等私密信息。
2. 解压本ZIP，把文件夹**内部所有内容**上传到该仓库根目录，包括 `.github/workflows/scan.yml`（注意 `.github` 是隐藏目录）。在Mac上可用Codex App打开该文件夹后，让它帮你推送GitHub。
3. 到仓库 **Settings → Actions → General → Workflow permissions**，选 **Read and write permissions**，保存；没有权限可能无法提交数据。
4. 在仓库 **Actions → AStockTail daily scan → Run workflow** 手动触发验证。**在非14:42~14:52执行时会留下`data_unavailable`而非假装给出实时选股。** 非交易日会直接退出。日志看 Actions 对应运行。
5. 登录 [Streamlit Community Cloud](https://share.streamlit.io)，点 **Create app**；选择上述GitHub仓库、`main` 分支，`app.py` 为入口，部署。获取类似 `https://xxx.streamlit.app` 的网站链接，即可手机/电脑访问。
6. 每个沪深交易日等待GitHub运行并写入 `data/YYYY-MM-DD.json`，网页可查看历史。GitHub->Actions还可以看失败日志。

### 可选：手机消息推送

GitHub仓库：`Settings → Secrets and variables → Actions → New repository secret`。添加以下任一组：
- `SERVERCHAN_SENDKEY`：Server酱个人SendKey
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`：Telegram机器人Token和接收方chat id

密钥绝不填在config.json或提交到仓库。未设密钥只保存历史，不推送。

## 2. 时间和筛选标准

- Cron `42 6 * * 1-5` 是 **UTC 06:42 = 北京时间14:42 = 韩国时间15:42**。程序等待到北京时间14:50附近，最迟14:52。若GitHub启动或行情API过晚会标记失败，不报假信号。GitHub Actions的schedule没有准点SLA。
- 先检查交易日历 `tool_trade_date_hist_sina`，休市不报。
- 快照：`stock_zh_a_spot_em`，沪深 60/68/00/30 股票；涨幅3–5%、量比>1、换手率5–10%、流通市值50–200亿元；快照只记录**获取时间**，不等于交易所最新逐股报价时间。
- 候选分钟K线：`stock_zh_a_hist_min_em(period="1")`，指数 `index_zh_a_hist_min_em(period="1")`。严格拒绝缺失、日期不对和过期的K线。
- 量价技术规则：下午13:50–14:10前段和14:20–14:40后段5分钟成交量比较，要求后段均量≥前段1.12倍、≥3个递增区间，且13:30–14:49最大单分钟成交量占比≤7.5%。
- 强于指数：上证/深成指/创业板/科创50分时对齐后，股价从首分钟到最后分钟的涨幅要大于对应指数。
- VWAP分时均价线：成交额累计 / (成交量手累计 ×100)，要求至少85%的分钟收盘价≥VWAP。
- 14:43–14:48创当日新高；后续到14:50之间必须真的有回踩（≥0.1%，≤1.8%），且该段的分钟低点未跌破同期VWAP（容差0.1%）。
- **以上是把图片里定性描述转成可复现的初版量化定义，建议先纸面验证，之后回测调整。**
- 严格通过不足3只不凑数。最多验证35个候选，但在公开免费行情/Actions运行时间限制下，不保证覆盖所有合格股票；可能0只或验证中断。

## 3. 重要限制

- GitHub Actions可能延迟或不执行，public repo连续60天无活动时工作流可被停用；Streamlit免费实例可能休眠。
- 免费AKShare公共接口可能变化、限流、对云机房IP封禁。第一次真实验证需看Actions日志；无法联网时不会生成假信号。
- 快照本身不包含交易所逐股时间戳，因此**不能证明每个快照指标精确是14:50的实时值**；如要求严格符合此条件，必须换带交易所时间戳的专业行情源。
- 工作流依次请求股票分钟数据，处理可能超过14:52导致仅验证部分股票；**这是一套研究用MVP，不是成熟的实时全市场策略引擎**。
- 公开GitHub仓库及Streamlit页面的选股历史对公众可见，不适合保存私密持仓数据。

## 4. 本地开发与测试

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
streamlit run app.py
# 只在真实交易日时间窗口运行：
python strategy.py
```

`python strategy.py --manual`是调试模式，会继续抓取实时数据但**不能当成14:50的实时选股结果**。请勿把调试结果作为投资依据。

## 5. 文件目录

- `app.py`：网页看板、历史记录与CSV下载
- `strategy.py`：筛选与通知
- `config.json`：量化阈值
- `.github/workflows/scan.yml`：交易日定时器、提交记录
- `data/`：GitHub自动保存的JSON
- `tests/`：基础单元测试

如果目标升级为真正14:50稳定覆盖全市场，推荐使用常驻云服务器并预先订阅分钟行情，或者购买专业数据API。策略尚未充分回测，不能保证次日上涨。
