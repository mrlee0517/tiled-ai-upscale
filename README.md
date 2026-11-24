# tiled-ai-upscale · AI 分块放大与细节修复

一个用于 Codex 的图像超分辨率技能。它将原图切成带重叠上下文的图块，逐块调用图像模型修复细节，再按指定像素尺寸拼合整图。适用于需要看清发丝、面部、服装纹理或产品边缘，同时希望保留原有构图、颜色和光线的图片。



![Uploading 对比.gif…]()


## 功能

- 默认使用 **4×4 十六宫格**，也支持 **3×3 九宫格**及其他行列组合。
- 按指定倍数放大宽和高，支持小数倍。例如，1680 × 2080 的原图放大 6 倍，输出 10080 × 12480。
- 用重叠图块保留边缘上下文。图块拼合前需对照原图检查位置，必要时先做几何配准。
- 支持局部蒙版修补、受保护区域校验，以及从最终 PNG 提取原始像素裁图，检查拼缝、双影和人物结构。
- 输出无损 PNG、整图预览和处理报告。源图带透明通道时保留透明度。不透明图同时导出 JPG。

## 安装与使用

1. 下载 [v1.0.0 技能整合包](https://github.com/mrlee0517/tiled-ai-upscale/releases/download/v1.0.0/tiled-ai-upscale-v1.0.0.zip) 并解压。
2. 把整个 `tiled-ai-upscale/` 文件夹放到 `~/.agents/skills/`。仅在一个项目使用时，可放到该项目的 `.agents/skills/` 中。确保 `SKILL.md` 位于技能目录的第一层。[Codex 技能文档](https://learn.chatgpt.com/docs/customization/overview#skills)
3. 在 Codex 附上原图，请求示例：

   > 使用 $tiled-ai-upscale 将这张图放大 6 倍，采用 3×3 九宫格。保留透明边缘和原有光色，检查发丝及拼缝有无双影。

整套流程需要可编辑输入图片的图像模型，优先使用当前 Codex 会话提供的 `image_gen`。本地脚本依赖 Python 3、Pillow 和 NumPy。已有环境可直接使用，也可通过 `python3 -m pip install Pillow numpy` 为当前 Python 环境安装依赖。

## 脚本用法

在技能目录中运行以下命令，将 `input.png` 换成实际原图路径。`prepare` 的工作目录需为空。

```bash
python3 scripts/grid_upscale.py prepare \
  --source input.png --scale 6 --rows 3 --cols 3 \
  --work work/upscale
```

对工作目录内的每个 `*_source.png` 进行 AI 编辑，保留裁图范围和长宽比。检查图块轮廓与原图是否对齐，并按 `manifest.json` 的命名保存对应的 `*_ai.png`。

```bash
python3 scripts/grid_upscale.py assemble \
  --work work/upscale --output outputs/photo_6x
```

脚本负责切块、色调匹配、融合与尺寸导出。细节重建由图像模型完成，模型返回尺寸随工具而定，末级会重采样到目标像素尺寸。成品的实际清晰度取决于原图质量、模型返回和配准效果。

## 文件

| 文件 | 内容 |
| --- | --- |
| [SKILL.md](SKILL.md) | Codex 技能入口和完整执行步骤 |
| [agents/openai.yaml](agents/openai.yaml) | 技能显示名称、简述和默认提示 |
| [scripts/grid_upscale.py](scripts/grid_upscale.py) | 切块、融合、导出和裁图检查 |
| [references/workflow.md](references/workflow.md) | 参数、文件命名、修补蒙版和裁图格式 |
| [references/quality.md](references/quality.md) | 图像编辑提示词、轮廓检查和双影检查 |
