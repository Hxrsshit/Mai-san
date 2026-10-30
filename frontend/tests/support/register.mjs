// Test-only loader: lets `node --test` import the app's .tsx components and
// follow its `@/` alias and extensionless imports, using only the declared
// `typescript` dev dependency. Type-only .ts files are still stripped by Node
// itself; this transpiles JSX (and nothing else) for .tsx.
import { existsSync, readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { dirname, resolve as resolvePath } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import ts from "typescript";

const root = resolvePath(dirname(fileURLToPath(import.meta.url)), "..", "..");
const EXTENSIONS = [".ts", ".tsx"];

function withExtension(base) {
  if (existsSync(base) && /\.(ts|tsx)$/.test(base)) return base;
  for (const extension of EXTENSIONS) {
    if (existsSync(base + extension)) return base + extension;
  }
  return null;
}

registerHooks({
  resolve(specifier, context, nextResolve) {
    let base = null;
    if (specifier.startsWith("@/")) {
      base = resolvePath(root, specifier.slice(2));
    } else if (specifier.startsWith(".") && context.parentURL?.startsWith("file:")) {
      const bare = specifier.split("?")[0];
      base = resolvePath(dirname(fileURLToPath(context.parentURL)), bare);
    }
    if (base !== null && !/\.(m?js|json|css)$/.test(base)) {
      const found = withExtension(base);
      if (found) {
        const query = specifier.includes("?") ? specifier.slice(specifier.indexOf("?")) : "";
        return { url: pathToFileURL(found).href + query, shortCircuit: true };
      }
    }
    return nextResolve(specifier, context);
  },
  load(url, context, nextLoad) {
    const path = url.split("?")[0];
    if (path.startsWith("file:") && path.endsWith(".tsx")) {
      const source = readFileSync(fileURLToPath(path), "utf8");
      const { outputText } = ts.transpileModule(source, {
        compilerOptions: {
          jsx: ts.JsxEmit.ReactJSX,
          module: ts.ModuleKind.ESNext,
          target: ts.ScriptTarget.ES2022,
        },
        fileName: fileURLToPath(path),
      });
      return { format: "module", source: outputText, shortCircuit: true };
    }
    return nextLoad(url, context);
  },
});
