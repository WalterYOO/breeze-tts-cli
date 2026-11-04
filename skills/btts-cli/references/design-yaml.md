# Design YAML 与 lint

## 最小结构

```yaml
schema_version: 1
name: dusk
language: zh
template: zh-late-night-radio/v1

identity:
  age: 30s
  gender_presentation: female
  timbre: [warm, clean, slightly_husky]
  pitch: low_mid
  articulation: clear, relaxed
  persona: late_night_host

delivery_baseline:
  pace: medium_slow
  energy: medium_low
  tone: calm, intimate

scene:
  proximity: close_mic
  room: dry_studio

avoid: [childlike, shouting]
covers:
  - "晚上好，欢迎收听今天的节目。"
  - "外面下着雨，我们慢慢聊。"
```

- `name` 必须匹配 `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`。
- `template` 的语言必须与 `language` 一致。
- `covers` 是真正拿来试听的短句，建议 2–3 秒语音，建议写 1–2 条。
  默认只使用第 1 条；`--wide` 使用前 2 条。写更多 cover 不会自动扩展矩阵。

## 字段

| 块 | 字段 | 说明 |
|---|---|---|
| `identity` | `age` | `teen` / `child` / `20s` / `30s` / `40s` / `50s` / `60s` / `senior` |
| | `gender_presentation` | `female` / `male` / `neutral` |
| | `timbre` | 可多个：`warm` `clean` `slightly_husky` `bright` `dark` `breathy` `nasal` `smooth` `crisp` `soft` `metallic` `deep` |
| | `pitch` | `low` / `low_mid` / `mid` / `high_mid` / `high` |
| | `articulation` | `clear` / `relaxed` / `precise` / `lazy` / `crisp` / `soft` |
| | `persona` | `late_night_host` `documentary_narrator` `anime_character` `podcast_host` `audiobook_narrator` `game_character` |
| `delivery_baseline` | `pace` | `slow` / `medium_slow` / `medium` / `medium_fast` / `fast` |
| | `energy` | `low` / `medium_low` / `medium` / `medium_high` / `high` |
| | `tone` | 可多个：`calm` `intimate` `warm` `cool` `playful` `serious` `gentle` `wry` |
| `scene` | `proximity` | `close_mic` / `mid_distance` / `far_field` |
| | `room` | `dry_studio` / `treated_room` / `natural_room` |
| `avoid` | 列表 | `childlike` `shouting` `robotic` `overly_bright` `monotone` 等 |
| `covers` | 列表 | 用于试听的短句，1–2 条 |

模板会把 token 映射成自然语言短语；未识别的值会以下划线转空格的形式回退，
并在 lint 的 `unknown-vocab` 里提示。宁可换成词表里的值。

## 编译产物

`compile_design` 保存三样东西：

1. `structured`：用户的结构化意图；
2. `rendered_instruction`：真正发给模型的字符串；
3. `compiler_version` 与 `template`。

run 和 profile 都记录这些字段，用于判断能否复现/resume。

## Lint 维度

| 维度 | 关注点 |
|---|---|
| `specificity` | identity / delivery / scene 的锚点是否齐全 |
| `executability` | 是否使用了难以映射到声学目标的模糊词 |
| `consistency` | 是否存在互相冲突的描述（如低能量 + 快语速） |
| `language_match` | instruction 和 covers 是否与 `language` 一致 |
| `testability` | 是否有足够短、可试听的 covers，以及 avoid/scene |

总分是五个维度的平均值。阈值只用于提示：≥85 绿、≥70 黄、否则红。

### block 级问题（必须修）

- `empty-instruction`：编译后的 instruction 为空。
- `inline-event`：instruction 里有括号事件标记。
- `language-mismatch`：语言与 instruction 实际用字不符。
- `dialogue-in-instruction`：某个描述字段超过 60 字符，像台词而不是描述。
- `no-anchors`：identity、delivery、scene 全空。

### 常见 warn / info

- `missing-anchors`：某些字段没有值。
- `vague-term`：出现「高级」「有质感」「professional」等模糊词。
- `conflict`：例如低音高却要求明亮、低能量却要求快语速，
  或 `age: child` 搭配成人主持类 persona。
- `no-covers` / `cover-length`：缺少 cover 或 cover 太长。
- `long-instruction`：instruction 超过 300 字符。
- `name-format`：name 不符合命名规则。
- `unknown-vocab`：token 不在词表中，已按回退文本渲染。
- `no-avoid`：没有 avoid 列表。

## 内联事件（括号标记）用在哪

| 位置 | 能否用 `[laugh]` / `[sigh]` 等 | 说明 |
|---|---|---|
| `say TEXT` 的朗读文本 | 可以 | `say` 不检查事件 |
| design instruction 字段 | 不可以 | lint `inline-event` 阻断 |
| `say --direction` | 不可以 | 退出码 2 |
| clone 的 reference transcript | 不可以 | `voice add` / freeze 拒绝 |

事件属于朗读文本，不属于音色描述或参考转写。

## 调 design 的顺序

1. 先保证 `identity` 有 2 个以上音色词、音高、年龄；
2. 再定 `delivery_baseline` 的语速/能量/语气；
3. 场景（近场/录音棚）最后调；
4. 每次只改一两个字段，重新 lint 和 explore，避免一次改太多无法归因。
