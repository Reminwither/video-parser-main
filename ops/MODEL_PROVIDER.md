# 从 ModelScope 免费推理切换到商业模型 API

当前生产环境使用 `https://api-inference.modelscope.cn/v1` 和 `Qwen/Qwen3.5-27B`。ModelScope 官方说明其免费推理接口不适合需要高并发或 SLA 保障的线上商业任务。因此公开放量之前，需要换到有正式计费与服务支持的接口。

现有应用已通过 OpenAI 兼容客户端调用模型。阿里云百炼北京地域提供同款 `qwen3.5-27b`，支持文本、图像和视频输入；接口和价格以官方文档为准：

- [OpenAI 兼容接口与北京地域地址](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)
- [视觉输入与模型能力](https://help.aliyun.com/zh/model-studio/vision-model)
- [模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)

切换时，在服务器 `/opt/video-parser/.env` 中设定：

```dotenv
QWEN_API_BASE_URL=https://<实际业务空间ID>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
QWEN_API_KEY=<百炼北京地域业务空间的 API Key>
QWEN_MODEL_ID=qwen3.5-27b
```

若设置过 `SYNTHESIS_MODEL_ID`、`QUALITY_MODEL_ID` 或 `ASR_CLEAN_MODEL_ID`，也要逐一改成该接口支持的模型 ID。不要把密钥提交到 Git。切换前记录旧配置并保留回滚方式；先在独立客户端验证纯文本、视觉输入和 JSON 输出，再按正常发布流程重启主站，使用有权使用的短视频完成“上传/解析 → 转写 → 分析 → 导出”，检查后台成功率和 token 统计。只有真实任务通过后再放开用户额度。

截至 2026-09-28，尚未配置百炼商业 API Key，生产环境仍为 ModelScope 免费推理。后台显示的百炼费用仅为按公开标价计算的替换方案参考值，不是当前实际扣费。
