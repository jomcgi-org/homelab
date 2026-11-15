/**
 * The compiled sheet theme. Import the theme from here, never from sheet.js.
 *
 * `astryx theme build` writes sheet.js with a sheet.d.ts beside it. Bazel's
 * js_library ships declaration files apart from sources, so a typecheck there
 * can see the JavaScript without its declaration and infer a wider type. The
 * cast states the type the build tool guarantees.
 */
import type { DefinedTheme } from "@astryxdesign/core/theme";
import { sheetTheme as built } from "./sheet.js";

export const sheetTheme = built as DefinedTheme;
