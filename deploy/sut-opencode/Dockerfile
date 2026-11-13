# RSI SUT Agent: OpenCode + 网络运维 skill + GLM(zhipuai-coding-plan) LLM 配置
# 平台经 tcp://<VM>:2375 驱动本镜像：/agent-config=config 版本，/trace=wiretap
# 真实 token 由 build.sh --build-arg 注入（defaults/opencode.json 内为占位符）
FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      openssh-client sshpass iputils-ping netcat-openbsd procps \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g opencode-ai@latest && npm cache clean --force

# LLM 配置 + 网络运维 skill 注入镜像（/opt/rsi-defaults 兜底；
# 运行时以挂载/注入的 /agent-config 优先）
ARG ZAI_TOKEN=placeholder
RUN mkdir -p /opt/rsi-src && true
COPY defaults/ /opt/rsi-defaults/
RUN sed -i "s/__ZAI_TOKEN__/${ZAI_TOKEN}/" /opt/rsi-defaults/opencode.json
ENV XDG_CONFIG_HOME=/rsi-config \
    ANTHROPIC_AUTH_TOKEN=${ZAI_TOKEN} \
    ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic \
    ANTHROPIC_URL=https://open.bigmodel.cn/api/anthropic \
    ANTHROPIC_MODEL=glm-5.3-flash
RUN mkdir -p /rsi-config/opencode /workspace /agent-config /trace/wiretap \
    && chmod +x /opt/rsi-defaults/skills/network-ops/huawei-cli.sh
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["sleep", "infinity"]
