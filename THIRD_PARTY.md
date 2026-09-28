# Сторонние компоненты

BadLink распространяется вместе со следующими компонентами без изменений.

| Компонент | Версия | Где лежит | Лицензия | Источник |
|---|---|---|---|---|
| WinDivert (WinDivert.dll, WinDivert64.sys) | 2.2.2 | `windivert/` | LGPLv3 (или GPLv2), текст — `windivert/LICENSE` | https://github.com/basil00/WinDivert |
| iperf3 (iperf3.exe) | 3.21 | `iperf3/` | BSD-3-Clause, текст — `iperf3/LICENSE-iperf3.txt` | https://github.com/esnet/iperf, Windows-сборка https://github.com/ar51an/iperf3-win-builds |
| Cygwin runtime (cygwin1.dll) | из сборки iperf3 3.21 | `iperf3/` | LGPLv3, текст — `iperf3/LICENSE-cygwin-LGPLv3.txt` | https://cygwin.com |

WinDivert и Cygwin используются как отдельные динамические библиотеки и могут быть заменены
пользователем на другие совместимые версии (требование LGPL).
