import { defineRailway, preserve, project, service } from "railway/iac";

export default defineRailway(() => {
  const reposageBackend = service("reposage-backend", {
    replicas: { "sfo": 1 },
    build: { builder: "DOCKERFILE", dockerfilePath: "docker/Dockerfile.backend" },
    env: { ANTHROPIC_API_KEY: preserve(), CLAUDE_MODEL: preserve(), GITHUB_TOKEN: preserve(), GMAIL_PASS: preserve(), GMAIL_USER: preserve(), GROQ_API_KEY_1: preserve(), GROQ_API_KEY_2: preserve(), GROQ_API_KEY_3: preserve(), GROQ_API_KEY_4: preserve(), GROQ_CLASSIFIER_MODEL: preserve(), GROQ_MODEL: preserve(), JINA_API_KEY: preserve(), JWT_SECRET: preserve(), MAX_FILES: preserve(), MAX_RETRIES: preserve(), MONGO_URL: preserve(), SERPER_API_KEY: preserve(), USE_MOCK: preserve(), WORKSPACE_DIR: preserve() },
  });

  return project("reposage-backend", {
    resources: [reposageBackend],
  });
});
