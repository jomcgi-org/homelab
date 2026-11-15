import contract from "./v1/snapshot.schema.json";
import type { Documents } from "./types";

// Interpret the deliberately small draft-07 subset used by this contract.
// No dynamic code generation: validation also runs under script-src 'self'.
// Unsupported keywords fail closed so extending the schema requires support here.
type Schema = {
  $ref?: string;
  type?: string;
  const?: unknown;
  enum?: unknown[];
  oneOf?: Schema[];
  properties?: Record<string, Schema>;
  propertyNames?: Schema;
  required?: string[];
  additionalProperties?: boolean | Schema;
  items?: Schema;
  uniqueItems?: boolean;
  pattern?: string;
  minimum?: number;
  maximum?: number;
};
const keywords = new Set([
  "$ref", "type", "const", "enum", "oneOf", "properties", "propertyNames",
  "required", "additionalProperties", "items", "uniqueItems", "pattern",
  "minimum", "maximum", "description",
]);
const definitions = contract.definitions as Record<string, Schema>;
const own = (value: object, key: string) => Object.hasOwn(value, key);

function matches(schema: Schema, value: unknown): boolean {
  if (Object.keys(schema).some((key) => !keywords.has(key))) return false;
  if (schema.$ref) {
    const name = schema.$ref.replace(/^#\/definitions\//, "");
    return own(definitions, name) && matches(definitions[name]!, value);
  }
  if (schema.oneOf) {
    return schema.oneOf.filter((branch) => matches(branch, value)).length === 1;
  }
  if (own(schema, "const") && value !== schema.const) return false;
  if (schema.enum && !schema.enum.includes(value)) return false;
  switch (schema.type) {
    case "object": {
      if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
      const record = value as Record<string, unknown>;
      if (schema.required?.some((key) => !own(record, key))) return false;
      return Object.entries(record).every(([key, item]) => {
        if (schema.propertyNames && !matches(schema.propertyNames, key)) return false;
        if (schema.properties && own(schema.properties, key)) {
          return matches(schema.properties[key]!, item);
        }
        if (schema.additionalProperties === false) return false;
        return typeof schema.additionalProperties !== "object" || matches(schema.additionalProperties, item);
      });
    }
    case "array":
      return Array.isArray(value) &&
        (!schema.uniqueItems || new Set(value.map((item) => JSON.stringify(item))).size === value.length) &&
        (!schema.items || value.every((item) => matches(schema.items!, item)));
    case "string":
      return typeof value === "string" && (!schema.pattern || new RegExp(schema.pattern, "u").test(value));
    case "integer":
    case "number":
      return typeof value === "number" && Number.isFinite(value) &&
        (schema.type !== "integer" || Number.isSafeInteger(value)) &&
        (schema.minimum === undefined || value >= schema.minimum) &&
        (schema.maximum === undefined || value <= schema.maximum);
    case undefined:
      return true;
    default:
      return false;
  }
}

export function validate<K extends keyof Documents>(kind: K, value: unknown): value is Documents[K] {
  return matches(definitions[kind]!, value);
}
