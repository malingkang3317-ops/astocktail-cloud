"""Read-only Streamlit dashboard; results committed by GitHub Actions."""
import json
from pathlib import Path
from datetime import datetime
import pandas as pd
import streamlit as st

ROOT=Path(__file__).resolve().parent
DATA=ROOT/'data'
st.set_page_config(page_title='AStockTail 尾盘选股',page_icon='📈',layout='wide')
st.title('📈 沪深A股尾盘筛选')
st.caption('北京时间14:50数据截止目标｜GitHub Actions + AKShare｜只展示通过实际检查的结果，非交易建议')

files=sorted([p for p in DATA.glob('20??-??-??.json')],reverse=True)
if not files:
    st.info('暂无真实运行记录。部署后先前往 GitHub → Actions 执行测试，或等到交易日。')
    st.stop()
records=[]
for f in files:
    try: records.append(json.loads(f.read_text(encoding='utf8')))
    except Exception: continue
if not records:
    st.error('历史JSON文件无法解析。');st.stop()
choices={r['date']:r for r in records}
day=st.selectbox('交易日',list(choices))
r=choices[day]
status_cn={'verified':'已验证部分候选','no_match_or_unverified':'无完全通过项 / 部分数据未验证',
           'data_unavailable':'数据或时效不足','closed':'休市','not_verified':'尚未完成验证'}
c1,c2,c3,c4=st.columns(4)
c1.metric('目标输出','最多3只')
c2.metric('严格入选',str(len(r.get('selected',[])))+'只')
c3.metric('候选验证',str(len(r.get('verified',[])))+'只')
c4.metric('任务状态',status_cn.get(r.get('status'),r.get('status')))
st.info('行情截止目标：'+r.get('cutoff','—')+'  ｜  运行记录：'+r.get('generated_at','—'))
st.warning('公开免费行情快照通常不包含交易所逐股时间戳。系统记录的是抓取时间，不能当作交易所实时性保证。GitHub Actions 也不能保证准点启动。')
st.subheader('完全符合的股票')
selected=r.get('selected',[])
if selected:
    cols=['代码','名称','最新价','涨跌幅','量比','换手率','流通市值亿元','分钟行情截止','指数','相对指数超额涨幅百分点','均价线上方比例']
    st.dataframe(pd.DataFrame(selected).reindex(columns=cols),hide_index=True,use_container_width=True)
else:
    st.info('本次没有可确认满足所有8项的股票，不凑数。')
st.subheader('详细判定')
for s in r.get('verified',[]):
    with st.expander(f"{s.get('代码')} {s.get('名称')} · {'全部通过' if s.get('完全符合') else '未通过'}"):
        st.json(s,expanded=True)
with st.expander('未通过 / 数据错误'):
    st.dataframe(pd.DataFrame(r.get('rejections',[])),hide_index=True,use_container_width=True)
st.subheader('历史结果')
history=pd.DataFrame([{'日期':x['date'],'状态':status_cn.get(x.get('status'),x.get('status')),
                       '合格数量':len(x.get('selected',[])),
                       '合格代码':', '.join(z['代码'] for z in x.get('selected',[])),
                       '运行时间':x.get('generated_at','')} for x in records])
st.dataframe(history,hide_index=True,use_container_width=True)
st.download_button('下载历史CSV',history.to_csv(index=False).encode('utf-8-sig'),file_name='astocktail_history.csv',mime='text/csv')
st.caption(r.get('message',''))
st.markdown('---')
st.caption('研究实验项目｜策略未经充分回测，不能保证次日上涨；不会执行任何交易。')
