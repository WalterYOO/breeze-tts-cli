# Voice、run 与完整性

## Voice 库

位置：`$BTTS_DATA_DIR/voices`，否则平台用户数据目录
（macOS: `~/Library/Application Support/btts_cli/voices`）。每个音色一个目录：

```text
voices/<name>/
  profile.json          # 结构化身份、默认值、provenance、license
  LICENSE.txt           # 模型许可证声明
  preview.wav           # 试听
  ref.wav               # 仅 clone
  ref.txt               # 仅 clone，逐字转写
  design.yaml           # 仅 design（来自 run 快照）
```

`profile.json` 的关键字段：

- `kind`: `design` | `clone`
- `identity`: 结构化 design + rendered instruction + template + compiler_version
- `design`: `seed` / `cfg_scale` / `instruction` / `max_new_tokens`（仅 design）
- `reference`: 音频/转写文件名、SHA-256、`transcript_confirmed`、时长、采样率（仅 clone）
- `defaults`: 默认 cfg 和 seed（`voice add` 的 seed 为 19；freeze 沿用源 take 的 seed）
- `provenance`: 来源 take/run、模型摘要、CLI 版本
- `license`: 模型许可证标识

写入是原子的：先复制到临时文件再 rename；最后写 `profile.json`。
`--force` 会覆盖同名 profile 的文件，但不会删除目录里的无关文件。

### 重命名、标签与删除

- **rename**：`btts voice rename OLD NEW` 把整个目录 `voices/OLD` 移到
  `voices/NEW`，并同步 `profile.json` 的 `name`。其余字段（`voice_id`、
  `tags`、`provenance`、`consent`、`license`、`reference` 的相对文件名）
  原样保留，目录内的文件也随之移动。实现顺序是先原子改写 profile，
  再对目录做同一文件系统内的 `os.rename`：若进程停在两步之间，目录仍叫
  `OLD` 而 profile 已写 `NEW`，重新执行同一条 rename 即可修复；
  `voice show OLD` 会以退出码 5 报告目录名/profile 名不一致并提示重跑
  rename，而不是打印 traceback；`voice list` 用目录名显示并打印同样的
  修复 warning。目标名已存在、源目录或 `profile.json` 是 symlink 时拒绝；
  目标目录不会被覆盖。目标检查与移动在同一 store 锁内，并使用 macOS
  `renamex_np(RENAME_EXCL)` / Linux `renameat2(RENAME_NOREPLACE)` 的
  no-replace 原语（不支持时降级为锁内 lexists + rename）。`voice add` 的
  `save` 也使用同一把锁并以非 `exist_ok` 的 `mkdir` 原子占名。
  APFS 等大小写不敏感文件系统上，仅大小写不同的改名会被拒绝并提示先用
  中间名；指向源目录的 symlink 目标按“已存在”处理（exit 2）。
  rename 会校验 clone 的 `ref.wav` / `ref.txt` 是否存在/逃逸，缺失时退出码 5，
  但不比较内容哈希。
- **tag**：`btts voice tag NAME` 不带操作时只列出 tags，不写文件，也不校验
  clone 参考文件；`--add/--remove/--set/--clear` 只原子改写 `profile.json`
  的 `tags` 字段，
  目录、参考音频和哈希都不变。`--set` 替换全部标签（每个 tag 一个 flag），
  `--clear` 先清空且可与 `--add` 组合，但两者都不能与 `--remove` 同用；
  `--set` 也不能与 `--add`/`--remove` 同用；`--clear` 与 `--set` 同样互斥。标签会去空白、去重且保持首次
  出现顺序，`--remove` 不存在的标签是幂等的；历史未规范化标签会在下一次
  变更时收敛。`voice add --tag` 使用同一套规范化规则。
- **delete**：`btts voice delete NAME` 删除 store 根目录内的单个音色目录，
  不做通配/批量删除。默认交互确认；非 TTY 环境必须由用户确认后传 `--yes`。
  只接受 store 根目录下的真实目录，名字校验拦截 `.`/`..` 和分隔符；目录或
  `profile.json` 是 symlink 时拒绝。POSIX 上删除会先把目录原子改名为
  `.deleting-<name>-<token>` tombstone，再通过 `O_NOFOLLOW` 目录描述符
  递归删除，绝不跟随 symlink。删除失败会 exit 5 并在 hint 中给出 tombstone
  路径；SIGKILL/断电没有提示，但 `voice list` 会跳过 tombstone 并在 stderr
  打印带路径的 warning，可用 `rm -rf "$STORE"/.deleting-*` 清理。
  删除不校验 clone 参考文件；删除后该音色从 `voice list` 消失，`voice show`
  报 voice not found。

### 导出

`btts voice export` 只读 store，不修改任何音色文件。`--mode full` 镜像完整音色
目录（所有非隐藏普通文件），`--mode wav` 只导出 `<name>.wav`（来自
`preview.wav`）。两种模式都写 `manifest.json`、`LICENSE.txt`、`README.txt`；
manifest 记录每个导出文件的相对路径、`sha256:` 和字节数，不含宿主机绝对路径。

- 选择：`--all` 或显式名字（可带 `@`，多个去重）；`--all` 遇到坏音色或无权限
  条目会整体失败（退出码 5），不会静默漏导。
- 默认输出目录 `./btts-voices-<UTC>`；`--zip` 输出单个 `.zip`，内含一个顶层
  目录。目标已存在（含 broken symlink）或位于 `voices/` 内时退出码 2；
  **没有 `--force`**。
- 导出前校验 profile、目录名一致性、clone `ref.wav`/`ref.txt` 哈希和
  `preview.wav`；symlink 或特殊文件退出码 5。失败时目标路径不会出现。
- `full` 可能含参考音频、逐字转写和 consent 元数据，按个人数据处理；`wav`
  的 manifest 仍保留音色元数据。
- 导出在 store 锁内完成，复制时按 dev/ino/size 和 SHA-256 复核；但递归遍历是
  路径式的，不是对非协作并发写入的事务快照。

### design vs clone

- **design**：保存配方（instruction + seed + cfg）。适合复现某次试听；
  换文本时音色可能漂移。
- **clone**：保存参考 WAV 和逐字转写。`say --voice` 走 `ref_clone_tata`，
  是跨文本最稳定的身份锚点。转写必须人工确认，CLI 不做自动转写。
- `say --direction` 走 `ref_edit_tata`，是一次性表演提示，不改变 profile。

## Run 目录

```text
runs/<run>/
  run.json                      # manifest：design 快照、矩阵、模型摘要、候选状态
  design.yaml                   # 创建 run 时的 design 快照
  candidates/
    c001.wav
    c001.recipe.json            # 输入文本、instruction、seed、cfg、模型摘要、耗时
    c002.wav
    c002.recipe.json
  report/
    metrics.csv
    report.md
    all_candidates.wav
    audition.html
```

- 每个候选先原子写 WAV，再写 recipe，再更新 `run.json`；因此中断后不会留下
  “看起来完整”的半成品。
- `run.json` 记录每个候选的 text SHA-256、模型摘要和完成状态。
- take id 形如 `c001`，必须匹配 `^c[0-9]{3,}$`。

## Resume 规则

`btts design explore FILE --out RUN_DIR --resume`：

- 只补齐缺失或哈希不匹配的候选；
- 沿用原 run 的候选矩阵；新传的 `--seeds`/`--cfgs`/`--wide` 会被忽略，
  `--max-new-tokens` 会改写所有计划的该字段；
- 如果 design 的 `rendered_instruction`、`template`、`covers` 与 run 创建时不同，
  拒绝 resume；
- 如果模型摘要变化，拒绝 resume；
- 想要尝试新 design、新矩阵、新模型或新生成参数，必须换新的 `--out`。

第一次 Ctrl-C 会等当前 take 完成后停止并标记 `interrupted`；第二次立即中断。
两种情况下都可以用 `--resume` 继续。

## 完整性校验

- `say` 的 clone/direction 路由会校验 `ref.wav` 和 `ref.txt` 的 SHA-256，
  不匹配退出码 5；`voice show` 做 schema、文件存在性和目录名/`profile.name`
  一致性检查，但不比较哈希；`voice list` 会跳过无效 profile、`.deleting-*`
  tombstone、隐藏条目、不可读目录和无法解析的 clone 参考文件（含 symlink loop），
  单个坏目录不会让整个 list 失败。`voice show` 对损坏/integrity 问题报退出码 5，
  对不可读目录报退出码 2，均不打印 traceback。
- `voice tag` 的 list 模式只校验 profile schema/文件名，不检查 clone 参考文件；
  变更模式会检查参考文件存在性/逃逸，但不比较内容哈希。
- `voice export` 比 `say` 更严格：会校验 clone 参考文件哈希和 `preview.wav`，
  拒绝 symlink 目录/profile/树内 symlink；`--all` 中任一坏音色都会让整个导出
  失败，而不是像 `voice list` 那样跳过。
- 只读路径（`list`/`show`/`say --voice`）目前允许跟随音色目录 symlink；
  `rename`/`tag`/`delete`/`export` 拒绝 symlink 目录和 symlink `profile.json`。
- store 根路径是普通文件时，list 返回空列表（exit 0），管理命令报 exit 2；
  store 根目录或其父目录存在但不可读（如 chmod 000）时，list 报 exit 2
  `cannot read voice store`，不会打印 traceback。
- clone 路由拒绝 `transcript_confirmed=false` 的 profile。
- `run.json` 和 `profile.json` 做的是 schema 校验与跨文件交叉校验
  （recipe/WAV、reference 路径），不是对文件自身的防篡改哈希。
- freeze 会拒绝：take 不完整、少于 1 秒、`finish_reason=length`
  或带截断 warning 的 take。
- 不要手改这些文件；需要修改时重新生成或重新 add。

## say 输出

未指定 `--out` 时：

```text
outputs/say-<name>-<UTC>.wav
outputs/say-<name>-<UTC>.recipe.json
```

recipe 记录 route、seed、cfg、instruction、模型摘要、时长和警告，
便于之后复现或排查。
