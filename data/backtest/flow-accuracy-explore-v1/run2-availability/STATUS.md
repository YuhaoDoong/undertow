# flow-accuracy-availability-v1-20261015 · run2（注记，非重算）

输入绑定：提交 `f58481be9a84616b12db41762f0e0754c232e575` 的树（信号文件、期权快照、run2/result.json）。日线 = unknown（未封存）。

| 品种 | preopen | after_open | unknown | 仅 preopen 行的开→收命中 |
|---|---|---|---|---|
| gold | 13 | 0 | 0 | 8/13 |
| silver | 12 | 0 | 0 | 6/12 |
| wti | 12 | 0 | 0 | 5/12 |
| qqq | 13 | 0 | 0 | 10/13 |
| tlt | 13 | 0 | 0 | 8/13 |
| spy | 13 | 0 | 0 | 7/13 |
| iwm | 12 | 0 | 0 | 3/12 |
| tqqq | 12 | 0 | 0 | 7/12 |

preopen = 两份输入快照（该提交中的版本）captured_at 都早于执行日 09:30 美东；只证明输入在开盘前已落盘，不证明信号行当时已算出。after_open / unknown 不得当作可执行样本。
二项 p / Wilson 假设各日独立同分布；本表属同一批数据上的多组探索，名义值不做多重校正、不得用来挑规则。
