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

