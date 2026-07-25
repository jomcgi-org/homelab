-- Adventure seed: boundaries classified from each book's section outline
-- (Claude Code in-session, 2026-07-05; see docs/plans/2026-07-05-grimoire-adventures.md
-- Task 4 and the grimoire-classify-adventures skill for the method).
-- start_seq/end_seq are knowledge_chunk seq ranges; end_seq NULL = to end of
-- book. Single-adventure books span the whole book (front matter and
-- appendices belong to their one adventure). Anthology boundaries were
-- located by matching each published adventure title against
-- section_hierarchy above the book's front-matter region, then verified
-- against the outline; trailing shared appendices are excluded.
-- planescape-adventures-in-the-multiverse is intentionally absent: only its
-- setting-guide volume is loaded so far (chunks end at Chapter 3), so it is
-- classified later via the skill once Turn of Fortune's Wheel lands.
-- Derived data: safe to re-seed (upsert on the natural key).

-- Parent book rows first: in prod these already exist (created by the chunk
-- loader) and DO NOTHING leaves them untouched, but a fresh database (the CI
-- test harness applies migrations to an empty Postgres) needs them for the
-- adventure book_id FK.
INSERT INTO grimoire.book (id, display_name)
VALUES
    ('curse-of-strahd', 'Curse Of Strahd'),
    ('lost-mine-of-phandelver', 'Lost Mine Of Phandelver'),
    ('rime-of-the-frostmaiden', 'Rime Of The Frostmaiden'),
    ('storm-kings-thunder', 'Storm Kings Thunder'),
    ('waterdeep-dragon-heist', 'Waterdeep Dragon Heist'),
    ('waterdeep-dungeon-of-the-mad-mage', 'Waterdeep Dungeon Of The Mad Mage'),
    ('tomb-of-annihilation', 'Tomb Of Annihilation'),
    ('descent-into-avernus', 'Descent Into Avernus'),
    ('the-wild-beyond-the-witchlight', 'The Wild Beyond The Witchlight'),
    ('candlekeep-mysteries', 'Candlekeep Mysteries'),
    ('tales-from-the-yawning-portal', 'Tales From The Yawning Portal'),
    ('keys-from-the-golden-vault', 'Keys From The Golden Vault'),
    ('ghosts-of-saltmarsh', 'Ghosts Of Saltmarsh')
ON CONFLICT (id) DO NOTHING;

INSERT INTO grimoire.adventure (book_id, name, seq, summary, level_range, start_seq, end_seq)
VALUES
    -- Single-adventure books (whole book, end_seq NULL absorbs future chunks)
    ('curse-of-strahd', 'Curse of Strahd', 1, 'Gothic horror sandbox in the mists of Barovia, hunting the vampire Strahd von Zarovich from the Death House prelude to Castle Ravenloft.', '1-10', 0, NULL),
    ('lost-mine-of-phandelver', 'Lost Mine of Phandelver', 1, 'The classic starter adventure: goblin ambushes on the Triboar Trail, the town of Phandalin, and the lost Wave Echo Cave.', '1-5', 0, NULL),
    ('rime-of-the-frostmaiden', 'Icewind Dale: Rime of the Frostmaiden', 1, 'Survival horror in an Icewind Dale locked in endless winter by Auril the Frostmaiden.', '1-12', 0, NULL),
    ('storm-kings-thunder', 'Storm King''s Thunder', 1, 'Giants ravage the North after the breaking of the ordning; the party is drawn into giant politics to restore order.', '1-11', 0, NULL),
    ('waterdeep-dragon-heist', 'Waterdeep: Dragon Heist', 1, 'An urban caper across Waterdeep chasing a vault of half a million gold dragons.', '1-5', 0, NULL),
    ('waterdeep-dungeon-of-the-mad-mage', 'Waterdeep: Dungeon of the Mad Mage', 1, 'The megadungeon of Undermountain: 23 levels of Halaster''s domain beneath the Yawning Portal.', '5-20', 0, NULL),
    ('tomb-of-annihilation', 'Tomb of Annihilation', 1, 'A death curse leads through the jungles of Chult to Acererak''s Soulmonger in the Tomb of the Nine Gods.', '1-11', 0, NULL),
    ('descent-into-avernus', 'Baldur''s Gate: Descent into Avernus', 1, 'From Baldur''s Gate into Avernus, first layer of the Nine Hells, to save the fallen city of Elturel.', '1-13', 0, NULL),
    ('the-wild-beyond-the-witchlight', 'The Wild Beyond the Witchlight', 1, 'A whimsical Feywild adventure from the Witchlight Carnival into the splintered domains of Prismeer.', '1-8', 0, NULL),

    -- Candlekeep Mysteries (17 adventures, front matter 0-70, contributor bios from 926)
    ('candlekeep-mysteries', 'The Joy of Extradimensional Spaces', 1, 'A scavenger hunt through Fistandia''s hidden extradimensional mansion, reached from a book in Candlekeep.', '1', 71, 109),
    ('candlekeep-mysteries', 'Mazfroth''s Mighty Digressions', 2, 'Counterfeit books that turn into monsters lead the party to a gnoll publishing ring outside Baldur''s Gate.', '2', 110, 157),
    ('candlekeep-mysteries', 'Book of the Raven', 3, 'A treasure map in a mysterious book leads to Chalet Brantifax and a conspiracy of wereravens.', '3', 158, 209),
    ('candlekeep-mysteries', 'A Deep and Creeping Darkness', 4, 'Investigating a mining village emptied by meenlocks lurking in the dark below.', '4', 210, 270),
    ('candlekeep-mysteries', 'Shemshime''s Bedtime Rhyme', 5, 'Quarantined in Candlekeep''s Firefly Cellar, the party must break the curse of a haunting nursery rhyme.', '4', 271, 333),
    ('candlekeep-mysteries', 'The Price of Beauty', 6, 'A magic mirror opens onto the Temple of the Restful Lily, a High Forest spa hiding a hag''s bargains.', '5', 334, 392),
    ('candlekeep-mysteries', 'Book of Cylinders', 7, 'Prophetic engraved cylinders send the party to defend a grippli village from yuan-ti.', '6', 393, 427),
    ('candlekeep-mysteries', 'Sarah of Yellowcrest Manor', 8, 'A ghost''s testimony draws the party into solving the murders of a Waterdhavian household.', '7', 428, 488),
    ('candlekeep-mysteries', 'Lore of Lurue', 9, 'The party is pulled into a storybook demiplane retelling how Silverymoon earned its name.', '8', 489, 520),
    ('candlekeep-mysteries', 'Kandlekeep Dekonstruktion', 10, 'Saboteurs scheme to launch one of Candlekeep''s towers skyward; the party must stop the countdown.', '9', 521, 570),
    ('candlekeep-mysteries', 'Zikran''s Zephyrean Tome', 11, 'A djinni bound in a tome bargains for freedom from the wizard who trapped him.', '10', 571, 615),
    ('candlekeep-mysteries', 'The Curious Tale of Wisteria Vale', 12, 'A pocket-dimension idyll built to contain a corrupted Harper whom the party must cure.', '11', 616, 681),
    ('candlekeep-mysteries', 'The Book of Inner Alchemy', 13, 'Stolen pages of a martial arts manual pit the party against the deadly monks of the Dawning Sun.', '12', 682, 728),
    ('candlekeep-mysteries', 'The Canopic Being', 14, 'In Tashluta, a would-be immortal harvests chosen organs; the party unravels the canopic scheme.', '13', 729, 774),
    ('candlekeep-mysteries', 'The Scrivener''s Tale', 15, 'Touching a cursed tome marks the party with a creeping doom they must trace across Faerun to undo.', '14', 775, 825),
    ('candlekeep-mysteries', 'Alkazaar''s Appendix', 16, 'A lost stone golem leads the party across the Anauroch desert on the trail of a Netherese legend.', '15', 826, 892),
    ('candlekeep-mysteries', 'Xanthoria', 17, 'A fungal plague sweeps the land; its cure lies with the lichen-lich druid Xanthoria.', '16', 893, 925),

    -- Tales from the Yawning Portal (7 classic dungeons, intro 0-34, appendices from 885)
    ('tales-from-the-yawning-portal', 'The Sunless Citadel', 1, 'A dungeon crawl beneath a sunken fortress where the Gulthias Tree and its twig blights fester.', '1-3', 35, 124),
    ('tales-from-the-yawning-portal', 'The Forge of Fury', 2, 'The dwarven stronghold of Khundrukar, now held by orcs, duergar, and the young black dragon Nightscale.', '3-5', 125, 226),
    ('tales-from-the-yawning-portal', 'The Hidden Shrine of Tamoachan', 3, 'A trap-laden Olman ziggurat slowly sinking into the swamp, explored room by deadly room.', '5', 227, 315),
    ('tales-from-the-yawning-portal', 'White Plume Mountain', 4, 'A funhouse dungeon quest to recover the sentient weapons Wave, Whelm, and Blackrazor.', '8', 316, 367),
    ('tales-from-the-yawning-portal', 'Dead in Thay', 5, 'An assault on the Doomvault, the Red Wizards'' sprawling gauntlet of linked dungeon zones.', '9-11', 368, 595),
    ('tales-from-the-yawning-portal', 'Against the Giants', 6, 'Three linked strikes on the hill, frost, and fire giant strongholds behind the raids on civilized lands.', '11', 596, 827),
    ('tales-from-the-yawning-portal', 'Tomb of Horrors', 7, 'Acererak''s legendary deathtrap tomb, restored in all its lethal glory.', '13', 828, 884),

    -- Keys from the Golden Vault (13 heists, intro 1-24, back matter from 909)
    ('keys-from-the-golden-vault', 'The Murkmire Malevolence', 1, 'Steal a dangerous egg from the Varkenbluff Museum of Natural History before it hatches.', '1', 25, 92),
    ('keys-from-the-golden-vault', 'The Stygian Gambit', 2, 'A casino heist against the Afterlife, a devil-themed gambling house, to reclaim a stolen prize.', '2', 93, 172),
    ('keys-from-the-golden-vault', 'Reach for the Stars', 3, 'Recover a spellbook from Delphi Mansion, a house warped by Far Realm star magic.', '3', 173, 246),
    ('keys-from-the-golden-vault', 'Prisoner 13', 4, 'Break into Revel''s End, the arctic panopticon prison, to extract a secret locked in Prisoner 13.', '4', 247, 309),
    ('keys-from-the-golden-vault', 'Tockworth''s Clockworks', 5, 'Free the svirfneblin town of Little Lockford from a tyrant''s clockwork enforcers.', '5', 310, 378),
    ('keys-from-the-golden-vault', 'Masterpiece Imbroglio', 6, 'Lift a coveted portrait out of a thieves'' guild den without waking the whole guild.', '5', 379, 451),
    ('keys-from-the-golden-vault', 'Axe from the Grave', 7, 'Return a murdered bard''s stolen axe to quiet his restless grave.', '6', 452, 518),
    ('keys-from-the-golden-vault', 'Vidorant''s Vault', 8, 'Crack the boastfully impenetrable vault of the jeweler Vidorant to reclaim a stolen diadem.', '7', 519, 573),
    ('keys-from-the-golden-vault', 'Shard of the Accursed', 9, 'Recover the Shard of Xeluan from beneath a grieving town before its curse consumes it.', '8', 574, 641),
    ('keys-from-the-golden-vault', 'Heart of Ashes', 10, 'Steal a dead ruler''s crystallized heart from a palace of ash and grief.', '8', 642, 688),
    ('keys-from-the-golden-vault', 'Affair on the Concordant Express', 11, 'An information heist aboard a planar train rolling through the Outlands.', '9', 689, 764),
    ('keys-from-the-golden-vault', 'Party at Paliset Hall', 12, 'Infiltrate an archmage''s glittering Feywild gala to steal a legendary diamond.', '10', 765, 852),
    ('keys-from-the-golden-vault', 'Fire and Darkness', 13, 'Steal the Book of Vile Darkness from an efreeti''s citadel on the Plane of Fire.', '11', 853, 908),

    -- Ghosts of Saltmarsh (7 nautical adventures; town chapters 0-222, Appendix A from 910)
    ('ghosts-of-saltmarsh', 'The Sinister Secret of Saltmarsh', 1, 'A haunted house on the cliffs conceals smugglers and their ship, the Sea Ghost.', '1-3', 223, 328),
    ('ghosts-of-saltmarsh', 'Danger at Dunwater', 2, 'Diplomacy or bloodshed at the lizardfolk lair arming for a hidden war beneath the waves.', '3', 329, 451),
    ('ghosts-of-saltmarsh', 'Salvage Operation', 3, 'Recover a merchant''s cargo from a derelict ship claimed by servants of Lolth.', '4', 452, 501),
    ('ghosts-of-saltmarsh', 'Isle of the Abbey', 4, 'Clear the skeleton-strewn abbey of a drowned cult to make way for a lighthouse.', '5', 502, 567),
    ('ghosts-of-saltmarsh', 'The Final Enemy', 5, 'Reconnaissance and assault on the sahuagin fortress massing against the coast.', '7', 568, 716),
    ('ghosts-of-saltmarsh', 'Tammeraut''s Fate', 6, 'Drowned undead rise from the Pit of Hatred to besiege a hermitage island.', '9', 717, 814),
    ('ghosts-of-saltmarsh', 'The Styes', 7, 'Murders in a rotting harbor district lead to an aboleth cult nurturing a juvenile kraken.', '11', 815, 909)
ON CONFLICT (book_id, name) DO UPDATE SET
    seq = EXCLUDED.seq,
    summary = EXCLUDED.summary,
    level_range = EXCLUDED.level_range,
    start_seq = EXCLUDED.start_seq,
    end_seq = EXCLUDED.end_seq;
