import coreWebVitals from "eslint-config-next/core-web-vitals";
import typescriptConfig from "eslint-config-next/typescript";

const config = [
  ...coreWebVitals,
  ...typescriptConfig,
  {
    rules: {
      // 06_ENGINEERING_RULES.md § Type Safety: no `any` on data crossing the
      // API boundary. An error rather than a warning, so it fails CI instead of
      // accumulating as noise nobody reads.
      "@typescript-eslint/no-explicit-any": "error",
    },
  },
  {
    ignores: [".next/**", "node_modules/**", "next-env.d.ts"],
  },
];

export default config;
