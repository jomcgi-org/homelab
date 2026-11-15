/** Shapes the session screen and its panels share. */
import type {
  CharacterView,
  LobbyCampaignView,
  LobbyUserView,
} from "../../api/schema";
import type { SheetDerived } from "../../api/sheets";

/** A character with the numbers from its latest approved sheet, if any. */
export interface TableCharacter extends CharacterView {
  approved: SheetDerived | null;
}

/** Who is looking: the campaign entry carries their role and character. */
export interface Viewer {
  user: LobbyUserView;
  campaign: LobbyCampaignView;
  dm: boolean;
}
