# Context-window strategy: measured

Scenario (brief section 4.5): ten W-2 images processed sequentially in one thread by the extraction step. `python run.py batch --no-context-strategy` replays every earlier document and reply in the prompt. `python run.py batch` uses selective recall: a rolling summary of the decisions so far plus the current document only (`ContextWindow` in `src/silverfile/memory.py`).

Model: `claude-sonnet-5-5`. Online: True. Measured 2026-10-25T20:22:20+00:00.

| Document | Input tokens, no strategy | Input tokens, selective recall | Output tokens (no / selective) |
| --- | ---: | ---: | --- |
| 1 (w2_01.jpg) | 2,436 | 2,453 | 131 / 147 |
| 2 (w2_02.jpg) | 4,922 | 2,532 | 131 / 146 |
| 3 (w2_03.jpg) | 7,408 | 2,614 | 131 / 146 |
| 4 (w2_04.jpg) | 9,894 | 2,696 | 131 / 146 |
| 5 (w2_05.jpg) | 12,380 | 2,778 | 131 / 146 |
| 6 (w2_06.jpg) | 14,866 | 2,860 | 131 / 146 |
| 7 (w2_07.jpg) | 17,352 | 2,942 | 131 / 146 |
| 8 (w2_08.jpg) | 19,838 | 3,024 | 131 / 146 |
| 9 (w2_09.jpg) | 22,324 | 3,106 | 135 / 150 |
| 10 (w2_10.jpg) | 24,814 | 3,190 | 135 / 150 |

Totals: 136,234 input tokens without the strategy, 28,195 with it (79% fewer). Last document: 24,814 vs 3,190. Growth from first to last document: +22,378 vs +737.

Replies that failed to parse as the requested JSON: 0 of 10 without the strategy, 0 of 10 with it.

Reading: without a strategy every earlier image and reply is replayed, so the prompt grows by roughly one image plus one reply per document and the curve climbs every step (an earlier run with text-only replay also saw the model drift from the requested JSON format on 8 of 10 documents; the image-replay run above did not). With selective recall the prompt is one summary line plus the current image, so it is flat; the small variation is the summary growing by one short decision per document until it is folded (rolling summarisation after 12 decisions).
