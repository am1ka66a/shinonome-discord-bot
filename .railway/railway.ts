import { defineRailway, project, service } from "railway/iac";
// This repository manages only its own resources in the environment. Other
// repositories export their own partial name.
// See https://docs.railway.com/infrastructure-as-code#multi-repo-projects
export const partial = "-";
export default defineRailway(() => {
  const resource = service("-", {
    build: { buildEnvironment: "V3", builder: "RAILPACK" },
    start: "python -u bot.py",
    deploy: { restartPolicyType: "ON_FAILURE", restartPolicyMaxRetries: 10 },
    healthcheck: "",
    replicas: { "us-west2": 1 },
    networking: { privateNetworkEndpoint: "service" },
  });

  return project("appealing-warmth", {
    variables: { managed: false },
    resources: [resource],
  });
});
