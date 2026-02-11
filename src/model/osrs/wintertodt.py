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

# Bank interface layout constants (OSRS bank is 488x300, centered on game_view)
BANK_INTERFACE_W = 488
BANK_INTERFACE_H = 300
# Deposit inventory button offset from bank interface origin (top-left of bank panel)
BANK_DEPOSIT_INV_X = 413
BANK_DEPOSIT_INV_Y = 281
# First bank item slot offset from bank interface origin
BANK_FIRST_SLOT_X = 57
BANK_FIRST_SLOT_Y = 77
BANK_SLOT_W = 36
BANK_SLOT_H = 32


class OSRSWintertodt(OSRSBot):
    # Wintertodt item IDs
    BRUMA_ROOT = ids.BRUMA_ROOT  # 20695
    BRUMA_KINDLING = ids.BRUMA_KINDLING  # 20696
    KNIFE = ids.KNIFE  # 946
    TINDERBOX = ids.TINDERBOX  # 590

    # Wintertodt animation IDs
    FLETCHING_ANIM = 1248  # FLETCHING_BOW_CUTTING
    FEEDING_ANIM = 832  # LOOKING_INTO
    IDLE_ANIM = -1

    # RuneLite tag colors (user must set these up)
    TAG_BRAZIER = clr.PINK
    TAG_ROOTS = clr.CYAN
    TAG_DOOR = clr.GREEN  # Tag the big doors green
    TAG_BANK = clr.RED  # Tag the bank chest red

    def __init__(self):
        bot_title = "Wintertodt"
        description = (
            "Plays the Wintertodt minigame with full round detection and banking.\n"
            "RuneLite setup: Tag bruma roots CYAN, brazier PINK, doors GREEN, bank chest RED.\n"
            "Start near the Wintertodt bank chest with an axe equipped.\n"
            "Requirements: 50 Firemaking."
        )
        super().__init__(bot_title=bot_title, description=description)
        # Option defaults
        self.running_time = 60
        self.hp_threshold = 15
        self.fletch_roots = False
        self.food_count = 5
        self.take_breaks = False
        # Runtime state
        self.state = WintertodtState.BANKING
        self.rounds_completed = 0
        self.round_active = False

    def create_options(self):
        self.options_builder.add_slider_option("running_time", "How long to run (minutes)?", 1, 500)
        self.options_builder.add_slider_option("hp_threshold", "Eat food when HP below?", 5, 50)
        self.options_builder.add_slider_option("food_count", "How many food to withdraw?", 1, 20)
        self.options_builder.add_checkbox_option("fletch_roots", "Fletch roots into kindling?", [" "])
        self.options_builder.add_checkbox_option("take_breaks", "Take breaks?", [" "])

    def save_options(self, options: dict):
        for option in options:
            if option == "running_time":
                self.running_time = options[option]
            elif option == "hp_threshold":
                self.hp_threshold = options[option]
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
        self.log_msg(f"Eat when HP below: {self.hp_threshold}.")
        self.log_msg(f"Food per round: {self.food_count}.")
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

        start_time = time.time()
        end_time = self.running_time * 60

        while time.time() - start_time < end_time:
            # --- HP check (always highest priority) ---
            if not self.__check_hp(api_m, api_s):
                return

            # --- Determine location and state ---
            in_arena = self.__is_in_arena(api_m)

            if not in_arena:
                # We're outside the arena (bank area)
                self.__handle_bank_area(api_m, api_s)
            else:
                # We're inside the arena
                self.__handle_arena(api_m, api_s)

            # Random break chance (only between rounds)
            if rd.random_chance(probability=0.02) and self.take_breaks and not self.round_active:
                self.take_break(max_seconds=15, fancy=True)

            self.update_progress((time.time() - start_time) / end_time)

        self.update_progress(1)
        self.log_msg(f"Finished. Rounds completed: {self.rounds_completed}")
        self.stop()

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
        Returns one of: 'round_end', 'brazier_out', 'interrupted', or 'none'.

        Chat strings verified against RuneLite WintertodtPlugin source:
          - "The brazier has gone out."                          -> brazier died
          - "The cold of"                                        -> cold damage interrupt
          - "The freezing cold attack"                           -> snowfall interrupt
          - "The brazier is broken and shrapnel"                 -> brazier damage interrupt
          - "You have run out of bruma roots"                    -> out of roots
          - "Your inventory is too full"                         -> inv full
          - "You fix the brazier"                                -> fixed brazier
          - "You light the brazier"                              -> lit brazier
          - "You carefully fletch the root"                      -> fletching

        Round end is detected by "subdued" in the message (from game broadcast).
        Round start is NOT reliably detected via chat — RuneLite uses a varbit timer.
        Instead, we assume the round is active when we're in the arena and can see
        tagged brazier/roots (fallback detection in __handle_arena).
        """
        try:
            msg = api_m.get_latest_chat_message()
        except Exception:
            return "none"

        if not msg:
            return "none"

        # Round end (game broadcast)
        if "subdued" in msg.lower():
            return "round_end"

        # Brazier went out (exact RuneLite string)
        if msg.startswith("The brazier has gone out"):
            return "brazier_out"

        # Interrupts from Wintertodt attacks
        if msg.startswith("The cold of") or msg.startswith("The freezing cold attack") or msg.startswith("The brazier is broken and shrapnel"):
            return "interrupted"

        return "none"

    # ==============================
    # Bank Area Logic
    # ==============================

    def __handle_bank_area(self, api_m: MorgHTTPSocket, api_s: StatusSocket):
        """Handle logic when outside the arena: bank if needed, then enter."""
        food_slots = api_s.get_inv_item_indices(ids.all_food)

        if len(food_slots) < self.food_count:
            # Need to bank: deposit junk, withdraw food
            self.__do_banking(api_m, api_s)
        else:
            # We have food, enter the arena
            self.__enter_arena(api_m)

    def __do_banking(self, api_m: MorgHTTPSocket, api_s: StatusSocket):
        """Open bank, deposit non-essential items, withdraw food."""
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

        # Deposit inventory, then withdraw food
        self.__deposit_inventory()
        time.sleep(0.8)

        self.__withdraw_food(api_s)
        time.sleep(0.5)

        # Close bank with Escape
        pag.press("escape")
        time.sleep(0.8)

        self.log_msg("Banking complete.")

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

    def __deposit_inventory(self):
        """
        Click the 'Deposit inventory' button in the bank interface.
        Position is calibrated: 488x300 bank panel, button at offset (413, 281).
        """
        self.log_msg("Depositing inventory...")
        bank_x, bank_y = self.__get_bank_origin()
        deposit_x = bank_x + BANK_DEPOSIT_INV_X
        deposit_y = bank_y + BANK_DEPOSIT_INV_Y
        self.mouse.move_to((deposit_x + rd.fancy_normal_sample(-3, 3),
                            deposit_y + rd.fancy_normal_sample(-3, 3)))
        time.sleep(0.3)
        # Verify via mouseover text
        if self.mouseover_text(contains="Deposit inventory", color=clr.OFF_ORANGE):
            self.mouse.click()
        elif self.mouseover_text(contains="Deposit", color=clr.OFF_ORANGE):
            self.mouse.click()
        else:
            self.log_msg("Could not verify deposit button. Clicking anyway.")
            self.mouse.click()

    def __withdraw_food(self, api_s: StatusSocket):
        """
        Withdraw food from the bank. Expects the user's food to be in the first bank slot
        (first tab, top-left item). Clicks the slot once per food item needed.
        Position is calibrated: first slot at offset (57, 77) within the 488x300 bank panel.
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
        """Walk through the big doors to enter the Wintertodt arena."""
        self.state = WintertodtState.ENTERING_ARENA
        self.log_msg("Entering Wintertodt arena...")

        # Find the doors (tagged GREEN)
        doors = self.get_all_tagged_in_rect(self.win.game_view, self.TAG_DOOR)
        if not doors:
            self.log_msg("No tagged doors found. Tag the Wintertodt doors GREEN.")
            time.sleep(2)
            return

        doors = sorted(doors, key=RuneLiteObject.distance_from_rect_center)
        self.mouse.move_to(doors[0].random_point())
        self.mouse.click()
        time.sleep(3)

        # Wait until we're actually inside
        for _ in range(10):
            if self.__is_in_arena(api_m):
                self.log_msg("Entered arena.")
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

        # Wait until we're outside
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

        if round_status == "interrupted":
            # Wintertodt attack interrupted our action — we're now idle, main loop will re-act
            pass

        # --- Fallback round detection ---
        # RuneLite uses a varbit for round start which we can't access via the HTTP API.
        # Instead, if we're in the arena and not marked as active, check if tagged objects
        # (roots/brazier) are visible — if so, assume the round is active.
        if not self.round_active:
            brazier = self.get_all_tagged_in_rect(self.win.game_view, self.TAG_BRAZIER)
            roots = self.get_all_tagged_in_rect(self.win.game_view, self.TAG_ROOTS)
            if brazier or roots:
                self.log_msg("Round appears active (tagged objects visible). Engaging.")
                self.round_active = True

        # --- If round is active, do Wintertodt actions ---
        if self.round_active:
            self.__do_wintertodt_actions(api_m, api_s)
        else:
            # Waiting for the round to start
            self.state = WintertodtState.WAITING_FOR_ROUND
            # Check if we have food; if not, exit and bank
            food_slots = api_s.get_inv_item_indices(ids.all_food)
            if len(food_slots) == 0:
                self.log_msg("No food. Leaving arena to bank.")
                self.__exit_arena(api_m)
                return
            time.sleep(2)

    def __do_wintertodt_actions(self, api_m: MorgHTTPSocket, api_s: StatusSocket):
        """Core Wintertodt gameplay: chop, fletch, feed."""
        has_roots = api_s.get_if_item_in_inv(self.BRUMA_ROOT)
        has_kindling = api_s.get_if_item_in_inv(self.BRUMA_KINDLING)
        inv_full = api_s.get_is_inv_full()

        if not api_m.get_is_player_idle():
            # Player is busy, wait a tick and let HP check handle interrupts
            time.sleep(1)
            return

        # Decide next action
        if inv_full or (has_roots and not self.fletch_roots) or has_kindling:
            # We have material to use
            if self.fletch_roots and has_roots:
                self.__fletch_roots(api_m, api_s)
            else:
                self.__feed_brazier(api_m, api_s)
        else:
            # Need more roots
            self.__chop_roots(api_m)

    def __on_round_end(self, api_m: MorgHTTPSocket, api_s: StatusSocket):
        """Handle round ending: update counter, exit arena to bank."""
        self.round_active = False
        self.rounds_completed += 1
        self.state = WintertodtState.ROUND_ENDING
        self.log_msg(f"Round complete! Total rounds: {self.rounds_completed}")

        # Wait a moment for the reward crate
        time.sleep(3)

        # Check if we have food for another round
        food_slots = api_s.get_inv_item_indices(ids.all_food)
        if len(food_slots) >= 2:
            # Enough food to stay for another round — wait in arena
            self.log_msg("Waiting for next round...")
            self.state = WintertodtState.WAITING_FOR_ROUND
        else:
            # Exit to bank for more food
            self.__exit_arena(api_m)

    # ==============================
    # Core Actions
    # ==============================

    def __chop_roots(self, api_m: MorgHTTPSocket):
        """Find and chop bruma roots (tagged CYAN in RuneLite)."""
        self.state = WintertodtState.CHOPPING
        self.log_msg("Chopping bruma roots...")

        roots = self.get_all_tagged_in_rect(self.win.game_view, self.TAG_ROOTS)
        if not roots:
            self.log_msg("No tagged bruma roots found. Tag roots CYAN.")
            time.sleep(2)
            return

        roots = sorted(roots, key=RuneLiteObject.distance_from_rect_center)
        target = roots[0]

        self.mouse.move_to(target.random_point())
        if not self.mouseover_text(contains="Chop", color=clr.OFF_WHITE):
            return
        self.mouse.click()
        time.sleep(0.5)

        self.__wait_while_active(api_m, timeout=15)

    def __fletch_roots(self, api_m: MorgHTTPSocket, api_s: StatusSocket):
        """Use knife on bruma roots to make kindling."""
        self.state = WintertodtState.FLETCHING
        self.log_msg("Fletching roots...")

        knife_slots = api_s.get_inv_item_indices(self.KNIFE)
        root_slots = api_s.get_inv_item_indices(self.BRUMA_ROOT)

        if not knife_slots or not root_slots:
            self.log_msg("Missing knife or roots for fletching.")
            return

        # Click knife
        self.mouse.move_to(self.win.inventory_slots[knife_slots[0]].random_point())
        self.mouse.click()
        time.sleep(0.3)

        # Click root
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
            self.log_msg("No roots or kindling to feed.")
            return

        self.log_msg("Feeding brazier...")

        brazier = self.get_all_tagged_in_rect(self.win.game_view, self.TAG_BRAZIER)
        if not brazier:
            self.log_msg("No tagged brazier found. Tag brazier PINK.")
            time.sleep(2)
            return

        brazier = sorted(brazier, key=RuneLiteObject.distance_from_rect_center)
        target = brazier[0]

        self.mouse.move_to(target.random_point())
        if not self.mouseover_text(contains="Feed", color=clr.OFF_WHITE):
            # Brazier might need lighting
            if self.mouseover_text(contains="Light", color=clr.OFF_WHITE):
                self.log_msg("Lighting brazier...")
                self.mouse.click()
                time.sleep(3)
                return
            return

        self.mouse.click()
        time.sleep(0.5)

        self.__wait_while_active(api_m, timeout=30)

    def __relight_brazier(self, api_m: MorgHTTPSocket):
        """Attempt to relight the brazier after it goes out."""
        brazier = self.get_all_tagged_in_rect(self.win.game_view, self.TAG_BRAZIER)
        if not brazier:
            self.log_msg("Cannot find brazier to relight.")
            time.sleep(1)
            return

        brazier = sorted(brazier, key=RuneLiteObject.distance_from_rect_center)
        self.mouse.move_to(brazier[0].random_point())

        if self.mouseover_text(contains="Light", color=clr.OFF_WHITE):
            self.mouse.click()
            time.sleep(3)
            self.log_msg("Brazier relit.")
        elif self.mouseover_text(contains="Feed", color=clr.OFF_WHITE):
            # Already lit by someone else
            self.log_msg("Brazier already relit by another player.")
            self.mouse.click()
            time.sleep(0.5)
        else:
            time.sleep(1)

    # ==============================
    # Support Functions
    # ==============================

    def __check_hp(self, api_m: MorgHTTPSocket, api_s: StatusSocket) -> bool:
        """Check HP and eat if below threshold. Returns False if out of food and HP critical."""
        try:
            current_hp, _ = api_m.get_hitpoints()
        except Exception:
            current_hp = self.get_hp()

        if current_hp == -1:
            return True

        if current_hp <= self.hp_threshold:
            self.log_msg(f"HP low ({current_hp}). Eating...")
            food_slots = api_s.get_inv_item_indices(ids.all_food)
            if not food_slots:
                self.log_msg("No food remaining!")
                # If we're in the arena, try to exit
                if self.__is_in_arena(api_m):
                    self.log_msg("Attempting to leave arena...")
                    self.__exit_arena(api_m)
                else:
                    self.log_msg("Stopping bot — no food and outside arena.")
                    self.stop()
                return False
            self.mouse.move_to(self.win.inventory_slots[food_slots[0]].random_point())
            self.mouse.click()
            time.sleep(1.5)
        return True

    def __wait_while_active(self, api_m: MorgHTTPSocket, timeout: int = 15):
        """Wait while the player is performing an action, with periodic HP and round checks."""
        start = time.time()
        while time.time() - start < timeout:
            if api_m.get_is_player_idle():
                break

            # Check for round end or interrupts while waiting
            round_status = self.__check_round_status(api_m)
            if round_status == "round_end":
                self.round_active = False
                break
            if round_status == "interrupted":
                # Wintertodt attack interrupted our action — break to re-evaluate
                break

            # Check HP while waiting
            try:
                current_hp, _ = api_m.get_hitpoints()
                if current_hp != -1 and current_hp <= self.hp_threshold:
                    break  # Exit so main loop handles eating
            except Exception:
                pass
            time.sleep(1)

    def __logout(self, msg: str):
        self.log_msg(msg)
        self.logout()
        self.stop()
