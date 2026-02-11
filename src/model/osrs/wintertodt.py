import time
from enum import Enum

import pyautogui as pag

import utilities.api.item_ids as ids
import utilities.color as clr
import utilities.random_util as rd
from model.osrs.osrs_bot import OSRSBot
from utilities.api.morg_http_client import MorgHTTPSocket
from utilities.api.status_socket import StatusSocket
from utilities.geometry import RuneLiteObject


class WintertodtState(Enum):
    GEARING_UP = "gearing_up"
    BANKING = "banking"
    ENTERING_ARENA = "entering_arena"
    WAITING_FOR_ROUND = "waiting_for_round"
    CHOPPING = "chopping"
    FLETCHING = "fletching"
    FEEDING = "feeding"
    ROUND_ENDING = "round_ending"
    EXITING_ARENA = "exiting_arena"


# Verified region IDs via RuneLite source + coordinate math:
# regionId = ((x >> 6) << 8) | (y >> 6)
# Arena at ~(1630, 3970) -> region 6462; Camp/bank at ~(1630, 3944) -> region 6461
# Boundary at y=3968 (Doors of Dinh)
WT_ARENA_REGION = 6462  # Confirmed: matches RuneLite WintertodtPlugin.WINTERTODT_REGION
WT_BANK_REGION = 6461

# Wintertodt respawn timer (fixed at 60 seconds since October 2024 rework)
WT_RESPAWN_SECONDS = 60

# Bank interface layout constants (OSRS bank is 488x300, centered on game_view)
BANK_INTERFACE_W = 488
BANK_INTERFACE_H = 300
# First bank item slot offset from bank interface origin
BANK_FIRST_SLOT_X = 57
BANK_FIRST_SLOT_Y = 77
BANK_SLOT_W = 36
BANK_SLOT_H = 32

# All axe item IDs (any of these equipped satisfies the axe requirement)
AXE_IDS = {
    ids.BRONZE_AXE,     # 1351
    ids.IRON_AXE,       # 1349
    ids.STEEL_AXE,      # 1353
    ids.BLACK_AXE,      # 1361
    ids.MITHRIL_AXE,    # 1355
    ids.ADAMANT_AXE,    # 1357
    ids.RUNE_AXE,       # 1359
    ids.DRAGON_AXE,     # 6739
    ids.INFERNAL_AXE,   # 13241
    ids.CRYSTAL_AXE,    # 23673
}

# Common warm clothing item IDs — need 4 equipped for max warmth damage reduction.
# This covers the most commonly used items. Not exhaustive but handles typical setups.
WARM_ITEM_IDS = {
    # Pyromancer outfit (from Wintertodt rewards)
    ids.PYROMANCER_HOOD,     # 20708
    ids.PYROMANCER_GARB,     # 20704
    ids.PYROMANCER_ROBE,     # 20706
    ids.PYROMANCER_BOOTS,    # 20710
    # Clue hunter outfit (free from beginner clues)
    ids.CLUE_HUNTER_GARB,    # 19689
    ids.CLUE_HUNTER_GLOVES,  # 19691
    ids.CLUE_HUNTER_TROUSERS,  # 19693
    ids.CLUE_HUNTER_BOOTS,   # 19695
    ids.CLUE_HUNTER_CLOAK,   # 19697
    # Staves (weapon slot warm items)
    ids.STAFF_OF_FIRE,       # 1387
    ids.FIRE_BATTLESTAFF,    # 1393
    ids.LAVA_BATTLESTAFF,    # 3053
    # Capes
    ids.FIRE_CAPE,           # 6570
    ids.FIREMAKING_CAPE,     # 9804
    ids.FIREMAKING_CAPET,    # 9805  (trimmed)
    ids.OBSIDIAN_CAPE,       # 6568
    # Other warm items
    ids.WARM_GLOVES,         # 20712
    ids.BRUMA_TORCH,         # 20720
    ids.TOME_OF_FIRE,        # 20714
    ids.TOME_OF_FIRE_EMPTY,  # 20716
    ids.SANTA_HAT,           # 1050
    ids.EARMUFFS,            # 4166
    ids.BOBBLE_HAT,          # 6856
    ids.WOOLLY_HAT,          # 6862
    ids.WOOLLY_SCARF,        # 6863
    ids.JESTER_HAT,          # 6858
    ids.FREMENNIK_GLOVES,    # 3799
    ids.FIREMAKING_HOOD,     # 9806
    ids.SANTA_JACKET,        # 12888
    ids.SANTA_PANTALOONS,    # 12889
    ids.SANTA_GLOVES,        # 12890
    ids.SANTA_BOOTS,         # 12891
}

# Items that also count as warm AND as an axe (weapon slot overlap)
WARM_AXE_IDS = {ids.INFERNAL_AXE}  # Infernal axe counts as both warm and an axe


class OSRSWintertodt(OSRSBot):
    # Wintertodt item IDs
    BRUMA_ROOT = ids.BRUMA_ROOT  # 20695
    BRUMA_KINDLING = ids.BRUMA_KINDLING  # 20696
    KNIFE = ids.KNIFE  # 946
    TINDERBOX = ids.TINDERBOX  # 590
    HAMMER = ids.HAMMER  # 2347

    # RuneLite tag colors (user must set these up)
    TAG_BRAZIER = clr.PINK
    TAG_ROOTS = clr.CYAN
    TAG_DOOR = clr.GREEN  # Tag the big doors green
    TAG_BANK = clr.RED  # Tag the bank chest red

    def __init__(self):
        bot_title = "Wintertodt"
        description = (
            "Plays the Wintertodt minigame (October 2024 rework mechanics).\n"
            "RuneLite setup: Tag bruma roots CYAN, brazier PINK, doors GREEN, bank chest RED.\n"
            "Start near the Wintertodt bank chest with axe equipped and 4 warm clothing pieces.\n"
            "Bring tinderbox, hammer, and knife (if fletching) in inventory.\n"
            "Bank should have food in the first slot (for warmth restoration).\n"
            "Requirements: 50 Firemaking."
        )
        super().__init__(bot_title=bot_title, description=description)
        # Option defaults
        self.running_time = 60
        self.eat_every_n_hits = 3
        self.fletch_roots = False
        self.food_count = 5
        self.take_breaks = False
        # Runtime state
        self.state = WintertodtState.BANKING
        self.rounds_completed = 0
        self.round_active = False
        self.round_ended_at = 0.0  # Timestamp when last round ended
        self.last_chat_msg = ""  # Track last-seen chat message to avoid stale repeats
        self.damage_count = 0  # Warmth damage events since last eat
        self.last_ate_at = 0.0  # Timestamp of last food/potion consumption

    def create_options(self):
        self.options_builder.add_slider_option("running_time", "How long to run (minutes)?", 1, 500)
        self.options_builder.add_slider_option("eat_every_n_hits", "Eat food after how many Wintertodt hits?", 1, 6)
        self.options_builder.add_slider_option("food_count", "How many food to withdraw per bank trip?", 1, 20)
        self.options_builder.add_checkbox_option("fletch_roots", "Fletch roots into kindling?", [" "])
        self.options_builder.add_checkbox_option("take_breaks", "Take breaks?", [" "])

    def save_options(self, options: dict):
        for option in options:
            if option == "running_time":
                self.running_time = options[option]
            elif option == "eat_every_n_hits":
                self.eat_every_n_hits = options[option]
            elif option == "food_count":
                self.food_count = options[option]
            elif option == "fletch_roots":
                self.fletch_roots = options[option] != []
            elif option == "take_breaks":
                self.take_breaks = options[option] != []
            else:
                self.log_msg(f"Unknown option: {option}")
                self.options_set = False
                return
        self.log_msg(f"Running time: {self.running_time} minutes.")
        self.log_msg(f"Eat after every {self.eat_every_n_hits} hits.")
        self.log_msg(f"Food per bank trip: {self.food_count}.")
        self.log_msg(f"Fletch roots: {'Yes' if self.fletch_roots else 'No'}.")
        self.log_msg("Options set successfully.")
        self.options_set = True

    def main_loop(self):
        api_m = MorgHTTPSocket()
        api_s = StatusSocket()

        # Open inventory tab
        self.log_msg("Selecting inventory...")
        self.mouse.move_to(self.win.cp_tabs[3].random_point())
        self.mouse.click()
        time.sleep(0.5)

        # --- Startup gear validation ---
        if not self.__validate_and_gear_up(api_m, api_s):
            self.log_msg("Failed to gear up. Stopping.")
            self.stop()
            return

        start_time = time.time()
        end_time = self.running_time * 60

        while time.time() - start_time < end_time:
            # --- Determine location and state ---
            in_arena = self.__is_in_arena(api_m)

            if not in_arena:
                self.__handle_bank_area(api_m, api_s)
            else:
                self.__handle_arena(api_m, api_s)

            # Random break chance (only between rounds)
            if rd.random_chance(probability=0.02) and self.take_breaks and not self.round_active:
                self.take_break(max_seconds=15, fancy=True)

            self.update_progress((time.time() - start_time) / end_time)

        self.update_progress(1)
        self.log_msg(f"Finished. Rounds completed: {self.rounds_completed}")
        self.stop()

    # ==============================
    # Gear Validation & Setup
    # ==============================

    def __validate_and_gear_up(self, api_m: MorgHTTPSocket, api_s: StatusSocket) -> bool:
        """
        Check if the player has all required gear equipped and in inventory.
        If items are missing, attempt to withdraw them from the bank.
        Returns True if all gear requirements are met, False if unrecoverable.
        """
        self.state = WintertodtState.GEARING_UP
        self.log_msg("Checking gear...")

        # --- Check equipped warm items ---
        warm_count = 0
        for item_id in WARM_ITEM_IDS | WARM_AXE_IDS:
            if api_m.get_is_item_equipped(item_id):
                warm_count += 1

        if warm_count < 4:
            self.log_msg(f"Warning: Only {warm_count}/4 warm items equipped. Damage will be higher.")
            self.log_msg("Equip 4 warm items for best warmth protection.")
            # Not a hard failure — player can still play, just takes more warmth damage

        # --- Check equipped axe ---
        has_axe_equipped = False
        for axe_id in AXE_IDS:
            if api_m.get_is_item_equipped(axe_id):
                has_axe_equipped = True
                break

        has_axe_in_inv = False
        if not has_axe_equipped:
            for axe_id in AXE_IDS:
                if api_s.get_if_item_in_inv(axe_id):
                    has_axe_in_inv = True
                    break

        # --- Check inventory tools ---
        has_tinderbox = api_s.get_if_item_in_inv(self.TINDERBOX) or api_s.get_if_item_in_inv(ids.BRUMA_TORCH)
        has_hammer = api_s.get_if_item_in_inv(self.HAMMER)
        has_knife = api_s.get_if_item_in_inv(self.KNIFE) if self.fletch_roots else True

        # --- Determine what's missing ---
        missing = []
        if not has_axe_equipped and not has_axe_in_inv:
            missing.append(("axe", list(AXE_IDS)))
        if not has_tinderbox:
            missing.append(("tinderbox", [self.TINDERBOX]))
        if not has_hammer:
            missing.append(("hammer", [self.HAMMER]))
        if not has_knife:
            missing.append(("knife", [self.KNIFE]))

        if not missing:
            self.log_msg(f"Gear check passed. Warm items: {warm_count}/4, axe: {'equipped' if has_axe_equipped else 'in inventory'}.")
            return True

        # --- Withdraw missing items from bank ---
        missing_names = [name for name, _ in missing]
        self.log_msg(f"Missing: {', '.join(missing_names)}. Withdrawing from bank...")

        if not self.__withdraw_missing_tools(api_s, missing):
            return False

        # Re-check after bank withdrawal
        has_tinderbox = api_s.get_if_item_in_inv(self.TINDERBOX) or api_s.get_if_item_in_inv(ids.BRUMA_TORCH)
        has_hammer = api_s.get_if_item_in_inv(self.HAMMER)
        has_knife = api_s.get_if_item_in_inv(self.KNIFE) if self.fletch_roots else True
        has_axe_in_inv = any(api_s.get_if_item_in_inv(axe_id) for axe_id in AXE_IDS)
        has_axe_equipped = any(api_m.get_is_item_equipped(axe_id) for axe_id in AXE_IDS)

        if not has_tinderbox or not has_hammer or (self.fletch_roots and not has_knife):
            self.log_msg("Still missing required tools after bank check.")
            return False

        if not has_axe_equipped and not has_axe_in_inv:
            self.log_msg("No axe found in bank or inventory.")
            return False

        self.log_msg("Gear check passed after bank withdrawal.")
        return True

    def __withdraw_missing_tools(self, api_s: StatusSocket, missing: list) -> bool:
        """
        Open bank and withdraw missing tools. Each entry in missing is (name, [item_ids]).
        Clicks the bank chest, then uses inventory check to find and withdraw each item.
        """
        # Open bank
        bank = self.get_all_tagged_in_rect(self.win.game_view, self.TAG_BANK)
        if not bank:
            self.log_msg("No tagged bank chest found. Tag the bank chest RED.")
            return False

        bank = sorted(bank, key=RuneLiteObject.distance_from_rect_center)
        self.mouse.move_to(bank[0].random_point())
        if not self.mouseover_text(contains="Bank", color=clr.OFF_WHITE):
            if not self.mouseover_text(contains="Use", color=clr.OFF_WHITE):
                self.log_msg("Could not open bank.")
                return False
        self.mouse.click()
        time.sleep(1.5)

        # Use bank search to find each missing item
        bank_x, bank_y = self.__get_bank_origin()
        # Bank search icon is at approximately (415, 40) from bank origin
        search_x = bank_x + 415
        search_y = bank_y + 40

        for name, item_ids in missing:
            self.log_msg(f"Searching bank for {name}...")

            # Click the search icon
            self.mouse.move_to((search_x + rd.fancy_normal_sample(-3, 3),
                                search_y + rd.fancy_normal_sample(-3, 3)))
            self.mouse.click()
            time.sleep(0.5)

            # Type the item name
            pag.typewrite(name, interval=0.05)
            time.sleep(0.8)

            # Click the first result (first bank slot position)
            slot_x = bank_x + BANK_FIRST_SLOT_X + (BANK_SLOT_W // 2)
            slot_y = bank_y + BANK_FIRST_SLOT_Y + (BANK_SLOT_H // 2)
            self.mouse.move_to((slot_x + rd.fancy_normal_sample(-3, 3),
                                slot_y + rd.fancy_normal_sample(-3, 3)))
            self.mouse.click()
            time.sleep(0.5)

            # Press Escape to close search, then verify
            pag.press("escape")
            time.sleep(0.3)

        # Close bank
        pag.press("escape")
        time.sleep(0.8)

        return True

    # ==============================
    # Location Detection
    # ==============================

    def __is_in_arena(self, api_m: MorgHTTPSocket) -> bool:
        """Check if the player is inside the Wintertodt arena using region ID."""
        try:
            _, _, region_id = api_m.get_player_region_data()
            return region_id == WT_ARENA_REGION
        except Exception:
            return False

    # ==============================
    # Round Detection
    # ==============================

    def __check_round_status(self, api_m: MorgHTTPSocket) -> str:
        """
        Check the latest chat message to detect round events.
        Only reacts to NEW messages (compares against last_chat_msg to avoid stale repeats).
        Returns one of: 'round_end', 'brazier_out', 'brazier_broken', 'damaged', or 'none'.

        Chat strings verified against RuneLite WintertodtPlugin source:
          - "The brazier has gone out."                          -> brazier extinguished
          - "The cold of"                                        -> standard cold damage
          - "The freezing cold attack"                           -> area attack damage
          - "The brazier is broken and shrapnel"                 -> brazier exploded (needs repair)
          - "You have run out of bruma roots"                    -> out of roots
          - "Your inventory is too full"                         -> inv full
          - "You fix the brazier"                                -> repaired brazier
          - "You light the brazier"                              -> lit brazier
          - "You carefully fletch the root"                      -> fletching

        Round end detected by "subdued" in broadcast message.
        Round start detected via fixed 60-second respawn timer after round end.
        """
        try:
            msg = api_m.get_latest_chat_message()
        except Exception:
            return "none"

        if not msg or msg == self.last_chat_msg:
            return "none"

        # New message — record it before processing
        self.last_chat_msg = msg

        # Round end (game broadcast)
        if "subdued" in msg.lower():
            return "round_end"

        # Brazier went out (exact RuneLite string)
        if msg.startswith("The brazier has gone out"):
            return "brazier_out"

        # Brazier exploded — needs hammer repair
        if msg.startswith("The brazier is broken and shrapnel"):
            return "brazier_broken"

        # Warmth damage from Wintertodt attacks
        if msg.startswith("The cold of") or msg.startswith("The freezing cold attack"):
            return "damaged"

        return "none"

    # ==============================
    # Warmth Management
    # ==============================

    def __handle_warmth(self, api_s: StatusSocket) -> bool:
        """
        Manage warmth by tracking damage events and eating food/potions.
        The warmth meter replaced HP in the October 2024 rework.
        Since the API doesn't expose warmth directly, we track incoming damage
        chat messages and eat after every N hits.
        Returns False if out of food and should leave arena.
        """
        if self.damage_count >= self.eat_every_n_hits:
            food_slots = api_s.get_inv_item_indices(ids.all_food)
            if not food_slots:
                self.log_msg("No food for warmth! Need to leave arena.")
                return False
            self.log_msg(f"Restoring warmth (took {self.damage_count} hits)...")
            self.mouse.move_to(self.win.inventory_slots[food_slots[0]].random_point())
            self.mouse.click()
            time.sleep(1.0)
            self.damage_count = 0
            self.last_ate_at = time.time()
        return True

    # ==============================
    # Bank Area Logic
    # ==============================

    def __handle_bank_area(self, api_m: MorgHTTPSocket, api_s: StatusSocket):
        """Handle logic when outside the arena: bank if needed, then enter."""
        food_slots = api_s.get_inv_item_indices(ids.all_food)

        if len(food_slots) < self.food_count:
            self.__do_banking(api_m, api_s)
        else:
            self.__enter_arena(api_m)

    def __do_banking(self, api_m: MorgHTTPSocket, api_s: StatusSocket):
        """Open bank, deposit non-essential items, withdraw food for warmth."""
        self.state = WintertodtState.BANKING
        self.log_msg("Banking...")

        # Find the bank chest (tagged RED)
        bank = self.get_all_tagged_in_rect(self.win.game_view, self.TAG_BANK)
        if not bank:
            self.log_msg("No tagged bank chest found. Tag the bank chest RED.")
            time.sleep(2)
            return

        bank = sorted(bank, key=RuneLiteObject.distance_from_rect_center)
        self.mouse.move_to(bank[0].random_point())

        # Verify mouseover says "Bank" or "Use"
        if not self.mouseover_text(contains="Bank", color=clr.OFF_WHITE):
            if not self.mouseover_text(contains="Use", color=clr.OFF_WHITE):
                time.sleep(0.5)
                return
        self.mouse.click()
        time.sleep(1.5)

        # Deposit non-tool items (keeps tinderbox, hammer, knife if fletching)
        self.__deposit_non_tools(api_s)
        time.sleep(0.8)

        # Withdraw food for warmth restoration
        self.__withdraw_food(api_s)
        time.sleep(0.5)

        # Close bank with Escape
        pag.press("escape")
        time.sleep(0.8)

        self.log_msg("Banking complete.")

    def __deposit_non_tools(self, api_s: StatusSocket):
        """
        Deposit all inventory items except essential tools.
        With the bank open, clicking an inventory item deposits it.
        Protected tools: tinderbox, hammer, knife (if fletching enabled).
        """
        tool_ids = {self.TINDERBOX, self.HAMMER}
        if self.fletch_roots:
            tool_ids.add(self.KNIFE)

        inv = api_s.get_inv()
        deposited = 0
        for item in inv:
            if item["id"] not in tool_ids:
                slot_idx = item["index"]
                self.mouse.move_to(self.win.inventory_slots[slot_idx].random_point())
                self.mouse.click()
                time.sleep(0.2)
                deposited += 1

        if deposited > 0:
            self.log_msg(f"Deposited {deposited} items.")

    def __get_bank_origin(self):
        """
        Calculate the top-left corner of the bank interface on screen.
        The OSRS bank panel (488x300) is centered on the game_view.
        Returns (x, y) screen coordinates of the bank panel's top-left corner.
        """
        gv = self.win.game_view
        bank_x = gv.left + (gv.width - BANK_INTERFACE_W) // 2
        bank_y = gv.top + (gv.height - BANK_INTERFACE_H) // 2
        return bank_x, bank_y

    def __withdraw_food(self, api_s: StatusSocket):
        """
        Withdraw food from the bank for warmth restoration.
        Expects food in the first bank slot (first tab, top-left item).
        Position calibrated: first slot at offset (57, 77) within the 488x300 bank panel.
        """
        self.log_msg(f"Withdrawing {self.food_count} food...")
        bank_x, bank_y = self.__get_bank_origin()
        slot_center_x = bank_x + BANK_FIRST_SLOT_X + (BANK_SLOT_W // 2)
        slot_center_y = bank_y + BANK_FIRST_SLOT_Y + (BANK_SLOT_H // 2)

        for _ in range(self.food_count):
            self.mouse.move_to((slot_center_x + rd.fancy_normal_sample(-5, 5),
                                slot_center_y + rd.fancy_normal_sample(-5, 5)))
            self.mouse.click()
            time.sleep(0.2)

        # Verify we got food
        time.sleep(0.3)
        food_slots = api_s.get_inv_item_indices(ids.all_food)
        if food_slots:
            self.log_msg(f"Withdrew {len(food_slots)} food.")
        else:
            self.log_msg("Warning: Could not verify food was withdrawn.")

    # ==============================
    # Arena Entry/Exit
    # ==============================

    def __enter_arena(self, api_m: MorgHTTPSocket):
        """Walk through the Doors of Dinh to enter the Wintertodt arena."""
        self.state = WintertodtState.ENTERING_ARENA
        self.log_msg("Entering Wintertodt arena...")

        doors = self.get_all_tagged_in_rect(self.win.game_view, self.TAG_DOOR)
        if not doors:
            self.log_msg("No tagged doors found. Tag the Wintertodt doors GREEN.")
            time.sleep(2)
            return

        doors = sorted(doors, key=RuneLiteObject.distance_from_rect_center)
        self.mouse.move_to(doors[0].random_point())
        self.mouse.click()
        time.sleep(3)

        for _ in range(10):
            if self.__is_in_arena(api_m):
                self.log_msg("Entered arena.")
                # Assume round is active on entry — if between rounds,
                # the 60-second timer logic will correct this quickly.
                self.round_active = True
                self.damage_count = 0
                return
            time.sleep(1)
        self.log_msg("Failed to enter arena.")

    def __exit_arena(self, api_m: MorgHTTPSocket):
        """Walk through the doors to exit back to the bank area."""
        self.state = WintertodtState.EXITING_ARENA
        self.log_msg("Exiting arena...")

        doors = self.get_all_tagged_in_rect(self.win.game_view, self.TAG_DOOR)
        if not doors:
            self.log_msg("No tagged doors found. Tag the Wintertodt doors GREEN.")
            time.sleep(2)
            return

        doors = sorted(doors, key=RuneLiteObject.distance_from_rect_center)
        self.mouse.move_to(doors[0].random_point())
        self.mouse.click()
        time.sleep(3)

        for _ in range(10):
            if not self.__is_in_arena(api_m):
                self.log_msg("Exited arena.")
                return
            time.sleep(1)
        self.log_msg("Failed to exit arena.")

    # ==============================
    # Arena Logic (Round Handling)
    # ==============================

    def __handle_arena(self, api_m: MorgHTTPSocket, api_s: StatusSocket):
        """Main arena logic — detect round status and act accordingly."""
        round_status = self.__check_round_status(api_m)

        # --- React to round events ---
        if round_status == "round_end":
            self.__on_round_end(api_m, api_s)
            return

        if round_status == "brazier_out":
            self.log_msg("Brazier went out! Relighting...")
            self.__relight_brazier(api_m)
            return

        if round_status == "brazier_broken":
            self.log_msg("Brazier destroyed! Repairing...")
            self.damage_count += 1  # Shrapnel also deals warmth damage
            self.__repair_brazier(api_m)
            return

        if round_status == "damaged":
            # Wintertodt attack reduced warmth — track it
            self.damage_count += 1
            self.round_active = True  # Damage confirms round is active

        # --- Warmth management ---
        if not self.__handle_warmth(api_s):
            self.__exit_arena(api_m)
            return

        # --- If round is active, do Wintertodt actions ---
        if self.round_active:
            self.__do_wintertodt_actions(api_m, api_s)
        else:
            self.state = WintertodtState.WAITING_FOR_ROUND

            # Check if we have food; if not, exit and bank
            food_slots = api_s.get_inv_item_indices(ids.all_food)
            if len(food_slots) == 0:
                self.log_msg("No food. Leaving arena to bank.")
                self.__exit_arena(api_m)
                return

            # Round starts exactly 60 seconds after the last one ended
            elapsed = time.time() - self.round_ended_at
            if self.round_ended_at > 0 and elapsed >= WT_RESPAWN_SECONDS + 5:
                self.log_msg("Round should have started (60s respawn elapsed). Engaging.")
                self.round_active = True
            else:
                time.sleep(2)

    def __do_wintertodt_actions(self, api_m: MorgHTTPSocket, api_s: StatusSocket):
        """Core Wintertodt gameplay: chop, fletch, feed."""
        has_roots = api_s.get_if_item_in_inv(self.BRUMA_ROOT)
        has_kindling = api_s.get_if_item_in_inv(self.BRUMA_KINDLING)
        inv_full = api_s.get_is_inv_full()

        if not api_m.get_is_player_idle():
            time.sleep(1)
            return

        # Decide next action
        if inv_full or (has_roots and not self.fletch_roots) or has_kindling:
            if self.fletch_roots and has_roots:
                self.__fletch_roots(api_m, api_s)
            else:
                self.__feed_brazier(api_m, api_s)
        else:
            self.__chop_roots(api_m)

    def __on_round_end(self, api_m: MorgHTTPSocket, api_s: StatusSocket):
        """Handle round ending: update counter, decide whether to stay or bank."""
        self.round_active = False
        self.rounds_completed += 1
        self.round_ended_at = time.time()
        self.state = WintertodtState.ROUND_ENDING
        self.log_msg(f"Round complete! Total rounds: {self.rounds_completed}")

        # Wait briefly for the round to fully resolve
        time.sleep(3)

        # Check if we have food for another round
        food_slots = api_s.get_inv_item_indices(ids.all_food)
        if len(food_slots) >= 2:
            self.log_msg("Waiting for next round (60s respawn)...")
            self.state = WintertodtState.WAITING_FOR_ROUND
        else:
            self.__exit_arena(api_m)

    # ==============================
    # Core Actions
    # ==============================

    def __chop_roots(self, api_m: MorgHTTPSocket):
        """Find and chop bruma roots (tagged CYAN in RuneLite)."""
        self.state = WintertodtState.CHOPPING

        roots = self.get_all_tagged_in_rect(self.win.game_view, self.TAG_ROOTS)
        if not roots:
            self.log_msg("No tagged bruma roots found. Tag roots CYAN.")
            time.sleep(2)
            return

        roots = sorted(roots, key=RuneLiteObject.distance_from_rect_center)
        self.mouse.move_to(roots[0].random_point())
        if not self.mouseover_text(contains="Chop", color=clr.OFF_WHITE):
            return
        self.mouse.click()
        time.sleep(0.5)

        self.__wait_while_active(api_m, timeout=15)

    def __fletch_roots(self, api_m: MorgHTTPSocket, api_s: StatusSocket):
        """Use knife on bruma roots to make kindling."""
        self.state = WintertodtState.FLETCHING

        knife_slots = api_s.get_inv_item_indices(self.KNIFE)
        root_slots = api_s.get_inv_item_indices(self.BRUMA_ROOT)

        if not knife_slots or not root_slots:
            self.log_msg("Missing knife or roots for fletching.")
            return

        self.mouse.move_to(self.win.inventory_slots[knife_slots[0]].random_point())
        self.mouse.click()
        time.sleep(0.3)

        self.mouse.move_to(self.win.inventory_slots[root_slots[0]].random_point())
        self.mouse.click()
        time.sleep(0.5)

        self.__wait_while_active(api_m, timeout=30)

    def __feed_brazier(self, api_m: MorgHTTPSocket, api_s: StatusSocket):
        """Feed roots or kindling into the brazier (tagged PINK in RuneLite)."""
        self.state = WintertodtState.FEEDING

        has_kindling = api_s.get_if_item_in_inv(self.BRUMA_KINDLING)
        has_roots = api_s.get_if_item_in_inv(self.BRUMA_ROOT)

        if not has_kindling and not has_roots:
            return

        brazier = self.get_all_tagged_in_rect(self.win.game_view, self.TAG_BRAZIER)
        if not brazier:
            self.log_msg("No tagged brazier found. Tag brazier PINK.")
            time.sleep(2)
            return

        brazier = sorted(brazier, key=RuneLiteObject.distance_from_rect_center)
        self.mouse.move_to(brazier[0].random_point())

        if self.mouseover_text(contains="Feed", color=clr.OFF_WHITE):
            self.mouse.click()
            time.sleep(0.5)
            self.__wait_while_active(api_m, timeout=30)
        elif self.mouseover_text(contains="Light", color=clr.OFF_WHITE):
            self.log_msg("Lighting brazier...")
            self.mouse.click()
            time.sleep(3)
        elif self.mouseover_text(contains="Repair", color=clr.OFF_WHITE):
            self.log_msg("Repairing brazier...")
            self.mouse.click()
            time.sleep(3)

    def __relight_brazier(self, api_m: MorgHTTPSocket):
        """Attempt to relight the brazier after it goes out."""
        brazier = self.get_all_tagged_in_rect(self.win.game_view, self.TAG_BRAZIER)
        if not brazier:
            time.sleep(1)
            return

        brazier = sorted(brazier, key=RuneLiteObject.distance_from_rect_center)
        self.mouse.move_to(brazier[0].random_point())

        if self.mouseover_text(contains="Light", color=clr.OFF_WHITE):
            self.mouse.click()
            time.sleep(3)
        elif self.mouseover_text(contains="Feed", color=clr.OFF_WHITE):
            # Already relit by another player
            self.mouse.click()
            time.sleep(0.5)

    def __repair_brazier(self, api_m: MorgHTTPSocket):
        """Repair a destroyed brazier using a hammer."""
        brazier = self.get_all_tagged_in_rect(self.win.game_view, self.TAG_BRAZIER)
        if not brazier:
            time.sleep(1)
            return

        brazier = sorted(brazier, key=RuneLiteObject.distance_from_rect_center)
        self.mouse.move_to(brazier[0].random_point())

        if self.mouseover_text(contains="Repair", color=clr.OFF_WHITE):
            self.mouse.click()
            time.sleep(3)
            self.log_msg("Brazier repaired.")
        elif self.mouseover_text(contains="Light", color=clr.OFF_WHITE):
            # Already repaired, needs lighting
            self.mouse.click()
            time.sleep(3)
        elif self.mouseover_text(contains="Feed", color=clr.OFF_WHITE):
            # Already repaired and lit by another player
            self.mouse.click()
            time.sleep(0.5)

    # ==============================
    # Support Functions
    # ==============================

    def __wait_while_active(self, api_m: MorgHTTPSocket, timeout: int = 15):
        """Wait while the player is performing an action, with periodic round and damage checks."""
        start = time.time()
        while time.time() - start < timeout:
            if api_m.get_is_player_idle():
                break

            # Check for round end, damage, or brazier events while waiting
            round_status = self.__check_round_status(api_m)
            if round_status == "round_end":
                self.round_active = False
                break
            if round_status in ("damaged", "brazier_broken"):
                self.damage_count += 1
                break  # Exit so main loop handles warmth
            if round_status in ("brazier_out", "brazier_broken"):
                break  # Exit so main loop handles brazier

            time.sleep(1)
