// @ts-check
import js from "@eslint/js";
import tseslint from "typescript-eslint";

/**
 * ESLint flat config for the LedgerLens TypeScript SDK.
 *
 * Baseline: eslint:recommended + @typescript-eslint/recommended.
 * Prettier owns formatting (see .prettierrc) — no stylistic rules here.
 */
export default tseslint.config(
  {
    ignores: ["dist/**", "node_modules/**", "coverage/**"],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.ts"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
    },
    rules: {
      // Allow intentionally-unused args/vars when prefixed with underscore.
      "@typescript-eslint/no-unused-vars": [
        "warn",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      // The SDK deliberately narrows `unknown` API payloads at runtime via Zod;
      // an occasional explicit `any` at those boundaries is acceptable.
      "@typescript-eslint/no-explicit-any": "warn",
    },
  },
  {
    // Tests may reach for looser typing and non-null assertions.
    files: ["tests/**/*.ts"],
    rules: {
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/no-non-null-assertion": "off",
    },
  },
);
