# Improvement pass A/B — #8 / #9 / #10

Same machine (Apple M4 mini), same isolated-Brave fixture, world-checked
through the fixture servers /events log (never the agents words).
Reproduce: `python3 bench/improve_bench.py --mode after`.
Baseline-only (no implementation): `--mode baseline`.

| #   | metric                                                    | before                      | after                | world / note |
|-----------------------------------------------------------------------------------------------------------------------------------|
| #10 | web(committed): digest chars                              | 447                         | 176                  |  |
| #10 | web(committed): digest lines                              | 15                          | 6                    |  |
| #10 | web(committed): non-actionable lines (chrome/structural)  | 6                           | 0                    |  |
| #10 | web(live Brave): digest chars                             | 895                         | 857                  |  |
| #10 | web(live Brave): digest lines                             | 30                          | 29                   |  |
| #10 | web(live Brave): non-actionable lines (chrome/structural) | 1                           | 0                    |  |
| #8  | shadow button click reaches it                            | False                       | True                 | before=False after=True |
| #8  | find-by-name sets shadow field                            | False                       | True                 | before=False after=True |
| #8  | verify catches no-op click (True=lies / False=honest)     | True                        | False                | /set/confirm fired=False (want False) |
| #8  | deep path still clicks light DOM                          | (n/a)                       | True                 | after=True |
| #9  | route(Brave bundle) -> tier                               | AX (always)                 | web                  |  |
| #9  | route(calculator bundle) -> tier                          | AX (always)                 | native               |  |
| #9  | route(AXWebArea snapshot) -> tier                         | AX (always)                 | web                  |  |
| #9  | page controls visible to AX vs DOM tier                   | AX: webarea=True ctrls=True | DOM: ctrls=True      | AX visibility focus-dependent; DOM always reads it |
| #9  | routed web->DOM completes a task                          | AX always (no route)        | tier=web, click=True | /set/shadowclick=True |

## Headline deltas
- **#10** web digest (committed App Store Connect capture): **447->176 chars, 15->6 lines, 6->0 non-actionable**. Native digest byte-identical (regression guard). Real 86KB tree: 17% char cut, all 80 element slots now actionable page controls.
- **#8** shadow-nested control: plain selector **False -> True** (deepQuery pierces; /events logs the click). find-by-accessible-name **False -> True**. verify catches a no-op click **True(lies) -> False(honest)**. Light DOM still clicks.
- **#9** router: brave->web, calc->native, AXWebArea->web. Routed web->DOM **completes a task end-to-end** (tier=web, click=True, /set/shadowclick logged). The 4B drove the shadow Save via the DOM loop in 1 turn, $0.
