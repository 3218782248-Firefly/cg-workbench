# CG Workbench

让个人创作者与 AI 协作制作 CG 时，**能恢复任务、能核对产物、能说清采用依据**。

Small, evidence-driven tools and workflows for resumable CG production. Python + optional Blender/FFmpeg. No API key, model service or paid DCC required for the first example.

这是一个小型、可读的 0.1 原型。源于一个角色短片项目的制作经验，公开示例全部使用程序生成的自制图形与几何。适合想把零散脚本整理为可靠执行流程的人；当前尚不是渲染农场、自动审美系统或任意 DCC 的生产级调度器。

## 先跑起来

需要 Python 3.11+。在仓库根目录：

```sh
python -m pip install -e .
cg-workbench run examples/synthetic/project.json --max-new-frames 3
# 上一步完成 3 帧后主动暂停，退出码 3；再次运行接续剩余帧。
cg-workbench run examples/synthetic/project.json
cg-workbench verify examples/synthetic/project.json
cg-workbench status .work/synthetic
```

示例为 12 帧、192×192、12fps。输出在 `.work/synthetic/<输入指纹>/`：PNG 序列、逐帧日志、输入清单、验收回执和紧凑状态。输出目录默认为仓库所在盘，正式项目请先按机器容量调整 `output`、磁盘保留量与预算。

可选视频，需要 FFmpeg 和 ffprobe 在 PATH 中：

```sh
cg-workbench encode examples/synthetic/project.json
```

生成 **无声 H.264 预览**，核对实际尺寸、解码帧数、帧率、音轨缺省状态并完整解码。此适配器不承担配乐、音画同步或后期调色。

## Blender 示例

安装 Blender 4.5，令 `blender` 可从 PATH 调用。或将 `examples/blender/project.json` 复制为同目录 `project.local.json`，把 `command` 第一项改成实际可执行文件路径。再运行：

```sh
cg-workbench run examples/blender/project.json
cg-workbench verify examples/blender/project.json
cg-workbench encode examples/blender/project.json
```

它生成带动画的蓝色陶瓷球、地面、灯光、相机以及 `demo.blend`，不访问原角色工程。每帧独立 Blender 进程用于隔离故障，启动开销较大；这是首版的明确取舍。需要已有角色工程时，提供自己的渲染脚本并声明所有 `.blend`、链接库、贴图和缓存输入，详见 [配置说明](docs/configuration.md)。

## 已实现

| 能力 | 实际行为 |
|---|---|
| 输入冻结 | 哈希配置、已声明输入、执行程序、执行器源码与运行时版本；变更后进入新目录 |
| 断点恢复 | 只复用有回执、哈希一致且重新解码合格的帧；损坏帧单独重做 |
| 有界监督 | 每个进程超时、运行时空间检查、有限空间等待/恢复；未知错误停止 |
| 互斥与状态 | OS 文件锁防止同一输出根重复执行，原子写入状态；记录实际已验帧数 |
| 技术验收 | PNG 完整性、像素解码、尺寸；可选明确校准的简单内容检查 |
| 可选预览 | FFmpeg 编码，实际视频帧数/帧率/全解码验证 |
| 同条件比较 | 原尺寸左右对照、相同坐标裁切、来源哈希；不偷偷缩放 |
| 可复用经验 | 三份精简 Skill、工作流、诊断案例、来源和审看模板 |

没有实现的能力列在 [路线与边界](docs/roadmap.md)。特别是：**技术通过不自动写成视觉通过或用户采用**。测试中的全黑图可以通过纯解码检查，加入针对该示例校准的内容规则后才会被拒绝。这个反例也是本项目最重要的说明之一。

## 文档入口

- [制作与判断工作流](docs/workflow.md)：参考、外观、动态、冻结、交付怎样衔接。
- [配置、恢复与状态](docs/configuration.md)：缓存何时失效、遇保护怎么接续、退出码含义。
- [故障反例与诊断方法](docs/failure-cases.md)：错误如何逃过验收，以及如何验证检查器。
- [来源与署名](docs/provenance.md)：通用代码、专用预设和图片素材分别记录。
- [验证记录](docs/validation.md)：已实测环境、覆盖范围和未验证范围。
- [贡献指南](CONTRIBUTING.md)：新增适配器、检查器和 Skill 的提交要求。

## Skills

仓库内 `skills/` 的三个目录可分别复制到支持 `SKILL.md` 的代理的技能目录；它们不依赖原项目历史，也不要求使用某个模型或账号：

- `cg-production`：参考与制作决策、保留已认可收益、按改动选择验证。
- `cg-output-review`：技术、内容、美术与采用分别记录，检查范围与证据绑定。
- `quality-recovery`：异常时排查竞争假设、按共享机制扩查、有界恢复。

仅需工具的人直接使用 CLI；没有必要为运行示例加载所有 Skill。

## 开发与许可

```sh
python -m unittest discover -s tests -v
```

测试会运行真实子进程，注入部分输出、损坏图、主体缺失、输入变更、超时和空间保护。CI 配置覆盖 Windows/Linux 与 Python 3.11/3.13；配置存在不等于这些环境已全部通过，实际记录见验证文档。

本仓库原创代码、文档和示例以 [MIT](LICENSE) 提供。外部运行时与依赖保留各自许可，见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。仓库不分发制作案例中的角色模型、动作、音乐、预设、贴图或第三方节点库。
