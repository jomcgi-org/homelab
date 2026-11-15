import {
  CheckboxList,
  CheckboxListItem,
} from "@astryxdesign/core/CheckboxList";
import { Selector } from "@astryxdesign/core/Selector";
import { TextArea } from "@astryxdesign/core/TextArea";
import type {
  EntityProjection,
  GrantScope,
  RevealedDetails,
} from "../api/knowledge";
import { knowledgeFields, selectedDetails } from "../lib/knowledge-fields.js";
import { fieldLabel } from "./KnowledgeDetails";

/** How much of an entity a grant shares, as the DM is choosing it. */
export interface ScopeDraft {
  scope: GrantScope;
  /** Entity fields picked for a partial grant. */
  fields: string[];
  /** Free text shared with a partial grant. */
  clue: string;
}

export const emptyDraft: ScopeDraft = {
  scope: "name_only",
  fields: [],
  clue: "",
};

const CLUE_LIMIT = 8000;

const SCOPES: Array<{ value: GrantScope; label: string }> = [
  { value: "name_only", label: "Name only" },
  { value: "partial", label: "Selected detail" },
  { value: "full", label: "Full knowledge" },
];

/** The details a partial grant would carry: the picked fields and the clue. */
export const detailsOf = (
  entity: EntityProjection,
  draft: ScopeDraft,
): RevealedDetails => selectedDetails(entity, draft.fields, draft.clue);

/** A partial grant must share something; the other scopes need no detail. */
export const isShareable = (draft: ScopeDraft, details: RevealedDetails) =>
  draft.scope !== "partial" || Object.keys(details).length > 0;

interface ScopeFieldsProps {
  entity: EntityProjection;
  value: ScopeDraft;
  onChange: (draft: ScopeDraft) => void;
}

/** The scope choice, and for a partial grant which fields and what clue. */
export function ScopeFields({ entity, value, onChange }: ScopeFieldsProps) {
  const keys = (knowledgeFields(entity) as Array<[string, unknown]>).map(
    ([key]) => key,
  );
  return (
    <>
      <Selector
        label="Knowledge scope"
        options={SCOPES}
        value={value.scope}
        onChange={(scope) => onChange({ ...value, scope: scope as GrantScope })}
        width="100%"
      />
      {value.scope === "partial" && keys.length > 0 && (
        <CheckboxList
          label="Fields to share"
          value={value.fields}
          onChange={(fields) => onChange({ ...value, fields })}
        >
          {keys.map((key) => (
            <CheckboxListItem key={key} value={key} label={fieldLabel(key)} />
          ))}
        </CheckboxList>
      )}
      {value.scope === "partial" && (
        <TextArea
          label="Detail to share"
          value={value.clue}
          onChange={(clue) =>
            onChange({ ...value, clue: clue.slice(0, CLUE_LIMIT) })
          }
          maxLength={CLUE_LIMIT}
          rows={3}
          width="100%"
        />
      )}
    </>
  );
}
