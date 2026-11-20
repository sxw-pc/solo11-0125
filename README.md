# 产品批次召回触达台

为产品流通质量专员与经销商提供离线合成公开来源的时效和证据核验。核心结果：判定受影响产品批次并追踪下架触达。公开文本经来源身份和版本采信后，分别驱动业务案件与履约核验；这三个子系统有独立业务实体、状态和 CLI 入口。

## 运行

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m recallcase demo
python -m pytest -q
```

手动运行：`python -m recallcase adopt --db state.db --input fixtures/recall_normal.json`，`python -m recallcase assess --db state.db --input fixtures/lineage_normal.json`，`python -m recallcase settle --db state.db --input fixtures/withdraw_normal.json`，`python -m recallcase report --db state.db --out report.json`。

## 状态与证据

- 召回采信：确认哪些批号和生效时点属于有效召回，留存原文摘要、修订、冲突和取消；同来源同版本异文不能覆盖旧证据。
- 批次传播：沿批次谱系决定哪些库存必须隔离，案件绑定来源修订，公共信息变更会把旧案件标为失效。
- 下架核销：逐渠道核销应隔离库存是否实际下架，回执逐项验版本、目标和时点；部分履约与逾期保留为独立结论。

正常和失败输入位于 `fixtures`。SQLite 使用事务保存新修订、业务评估与回执；`report` 从库中重建来源摘要、案件和履约状态。模拟来源不代表真实法律、医疗或安全结论；生产接入需要对外部来源真实性和凭据管理另行评估。

## 渠道库存调拨

`stock-load` 登记经销商批次与父批号；`transfer` 独立裁定调拨是 accepted、held、blocked 还是 refused。有效召回的受影响批次会被持有，取消后才可转移；后续再次召回时，目标经销商的库存产生新的下架义务。`stock_lot` 与 `transfer_order` 独立持久，调拨会使相关库存评估失效。`fixtures/stock_normal.json`、`fixtures/stock_conflict.json` 展示同一批次在召回、取消与再次召回下不同的调拨和履约结果。

同一时窗的有效召回令若批号范围重叠，库存评估会并入关联范围并取最早下架截止；新令采信令旧库存决定失效。混包中只要一个成分追溯到召回批号，整包数量需隔离；来源链不完整或成环则给出不确定，不能用缺失谱系推断豁免。对应复现见 `fixtures/recall_overlap.json` 与 `tests/test_decisions.py`。

一批库存分布于多个渠道时，输入 `facts.channels` 的数量必须合计原库存；每个渠道形成独立下架义务。一个渠道回执完成但另一个缺失仍为部分完成，直到所有渠道各自提供正确批号、封签和数量证明。

## 离线质量投诉采集

`complaint-import --db state.db --input fixtures/complaint_normal.json` 将虚构热线和经销商记录保存为独立证据。来源版本与原始字节固定，同版本异文记为冲突，重复导入不增加记录。`complaint-watch --db state.db --threshold 2` 按明确的 case_ref 去重，只给出按产品、批号原文和缺陷代码分组的初步观察数。这条观察线不更改正式召回范围或下架义务。

## 投诉率监测（销售暴露量）

在按条数观察之外，`watch-evaluate` 用销售暴露量做可复核的异常率监测；它只决定是否开启质量调查，不修改正式召回令、库存隔离或厂家收讫。输入依次为：

- `watch-charter --db state.db --product P1 --baseline 0.001 --budget 0.05 --at 2026-05-31T00:00:00Z`：在首个销售日之前登记章程，确定基准投诉率和整期误报预算（`--period-days` 默认 7）。登记后同产品再改参数报 `watch_charter_conflict`，已有销售日再登记报 `watch_charter_after_sales`。
- `sales-import --db state.db --input fixtures/sales_normal.json`：销售文件含 `source_id`、`version` 和 `days`；每个销售日给出 `product`、`lot_text`、`sale_date`、实际售出件数 `units` 和来源摘要 `summary`。同一（产品、批号、售出日）异文记 `sales_day_conflict`，同版本异文记 `sales_source_conflict`，重放不增记录。
- `closeout-import --db state.db --input fixtures/closeout_normal.json`：结卷文件含 `credentials`；每条凭证给出 `id`、`product`、`lot_text`、`sale_date`、`channel`（`hotline` 或 `retailer`）和 `closed_at`。一个销售日两条投诉来源都结卷才算完整结卷；缺一方时该日不可判定，缺失一方不能当成零投诉。
- 投诉记录可带 `sold_on`（售出日，ISO 日期），把 case_ref 对到所属销售日和批号；同一件经热线、经销商各报一次只算一例。
- `watch-evaluate --db state.db --at 2026-06-02T00:00:00Z`：对已完整结卷且未待核的日子作逐日检验，结果另存 `watch_decision`／`watch_investigation`／`watch_review`，重跑不重复判定。

两种章程共用同一基准率和整期预算，各自独立判定：先期开查（`early`）每天看一次，第 d 天只能用预算的 1/[d(d+1)] 对累计已结卷证据作精确二项尾部检验（单侧，P[X≥k]，X~Binomial（售出件数， 基准率）），一旦开查，即使后来销量稀释也不撤回；结周（`week`）只在七个销售日都结卷后对合并数据做一次同样的单侧检验，用掉全部预算，不拿日内最小概率值事后挑一天。以基准率千分之一、预算 0.05、首日售出 1000 件有 5 件独立投诉、随后六日各售出 1000 件无新投诉为例：首日尾部概率约 0.00364，低于当日 0.025 的额度，先期开查签发调查；整周 5/7000 的尾部概率约 0.827，结周不开查。两种决定都正确，不是同一计数换个展示。

下列证据留作待核（`watch_review`），不挤进已签发的统计：售出为零的销售日及其投诉（`zero_sales_day`／`zero_sales_attribution`）、去重投诉数超过售出量的销售日（`complaints_exceed_sales`）、同一 case_ref 批号或售出日归属矛盾（`lot_attribution_conflict`）、无法对到销售日的记录（`sale_day_unattributed`）、结卷后才报来的记录（`late_after_closeout`）、签发统计后才导入的当日记录（`signed_off_before_import`）。已签发的每日分子分母随之冻结。

`report` 的 `watch_decisions` 重建每次判定的分子、分母、尾部概率、当日额度、已消耗额度、判定时点及采用的销售文件与结卷凭证；`watch_investigations` 记录调查是否已签发；`watch_days` 给出每个销售日的结卷与签发状态——未完整结卷的日子 `signed_cases` 为 null，未观察到投诉不等于已证明零投诉。复现见 `fixtures/sales_normal.json`、`fixtures/closeout_normal.json`、`fixtures/complaint_rated.json` 与 `tests/test_watch.py`。

