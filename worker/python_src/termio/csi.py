from __future__ import annotations

from .ansi import CSI, ESC


def sequence(final: str, *params: int | str) -> str:
    joined = ";".join(str(param) for param in params)
    return f"{CSI}{joined}{final}"


def cursor_home() -> str:
    return f"{CSI}H"


def cursor_position(row: int, column: int) -> str:
    return sequence("H", max(1, row), max(1, column))


def clear_screen() -> str:
    return f"{CSI}2J"


def erase_in_line(mode: int = 2) -> str:
    return sequence("K", mode)


def scroll_up(lines: int = 1) -> str:
    return sequence("S", max(1, lines))


def scroll_down(lines: int = 1) -> str:
    return sequence("T", max(1, lines))


def set_scroll_region(top: int, bottom: int) -> str:
    return sequence("r", top, bottom)


def reset_scroll_region() -> str:
    return f"{CSI}r"


def show_cursor() -> str:
    return f"{CSI}?25h"


def hide_cursor() -> str:
    return f"{CSI}?25l"


def paste_start() -> str:
    return f"{CSI}200~"


def paste_end() -> str:
    return f"{CSI}201~"


def focus_in() -> str:
    return f"{CSI}I"


def focus_out() -> str:
    return f"{CSI}O"


def enable_kitty_keyboard() -> str:
    return f"{CSI}>1u"


def disable_kitty_keyboard() -> str:
    return f"{CSI}<u"


def enable_modify_other_keys() -> str:
    return f"{CSI}>4;2m"


def disable_modify_other_keys() -> str:
    return f"{CSI}>4m"


def cursor_up(lines: int = 1) -> str:
    return sequence("A", max(1, lines))


def cursor_down(lines: int = 1) -> str:
    return sequence("B", max(1, lines))


def cursor_forward(columns: int = 1) -> str:
    return sequence("C", max(1, columns))


def cursor_back(columns: int = 1) -> str:
    return sequence("D", max(1, columns))


def cursor_position_absolute(row: int) -> str:
    return sequence("d", max(1, row))


def cursor_position_horizontal(column: int) -> str:
    return sequence("G", max(1, column))


def line_position_absolute(line: int) -> str:
    return sequence("H", 1, max(1, line))


def erase_to_end_of_line() -> str:
    return sequence("K", 0)


def erase_to_start_of_line() -> str:
    return sequence("K", 1)


def erase_line() -> str:
    return sequence("K", 2)


def erase_to_end_of_screen() -> str:
    return f"{CSI}0J"


def erase_to_start_of_screen() -> str:
    return f"{CSI}1J"


def erase_screen() -> str:
    return f"{CSI}2J"


def erase_scrollback() -> str:
    return f"{CSI}3J"


def erase_lines(lines: int = 1) -> str:
    result = ""
    for _ in range(lines):
        result += f"{CSI}M"
    return result


def device_status_report() -> str:
    return f"{CSI}6n"


def device_status_report_extended(param: int = 0) -> str:
    return sequence("n", param)


def cursor_position_report() -> str:
    return f"{CSI}6n"


def save_cursor() -> str:
    return f"{ESC}7"


def restore_cursor() -> str:
    return f"{ESC}8"


def dec_save_cursor() -> str:
    return f"{ESC}7"


def dec_restore_cursor() -> str:
    return f"{ESC}8"


def set_margins(top: int, bottom: int) -> str:
    return sequence("r", max(1, top), bottom)


def top_bottom_margins(top: int, bottom: int) -> str:
    return sequence("r", max(1, top), bottom)


def reset_mode() -> str:
    return f"{CSI}0m"


def set_mode(mode: int) -> str:
    return sequence("h", mode)


def set_private_mode(mode: int) -> str:
    return f"{CSI}?{mode}h"


def reset_private_mode(mode: int) -> str:
    return f"{CSI}?{mode}l"


def set_keypad_mode(application: bool = True) -> str:
    return f"{CSI}={'=' if application else '>'}="


def set_application_keypad() -> str:
    return f"{CSI}="


def set_numeric_keypad() -> str:
    return f"{CSI}>"


def reset_attributes() -> str:
    return f"{CSI}0m"


def bold_on() -> str:
    return f"{CSI}1m"


def italic_on() -> str:
    return f"{CSI}3m"


def underline_on() -> str:
    return f"{CSI}4m"


def reverse_on() -> str:
    return f"{CSI}7m"


def hidden_on() -> str:
    return f"{CSI}8m"


def strikethrough_on() -> str:
    return f"{CSI}9m"


def forward_tab(n: int = 1) -> str:
    return sequence("I", max(1, n))


def backward_tab(n: int = 1) -> str:
    return sequence("Z", max(1, n))


def tab_set() -> str:
    return f"{CSI}H"


def tab_clear() -> str:
    return f"{CSI}0g"


def tab_clear_all() -> str:
    return f"{CSI}3g"


def line_feed() -> str:
    return f"{CSI}0M"


def reverse_line_feed() -> str:
    return f"{CSI}1M"


def designate_g0_charset(charset: str) -> str:
    return f"{ESC}({charset}"


def designate_g1_charset(charset: str) -> str:
    return f"{ESC}){charset}"


def report_window_title() -> str:
    return f"{CSI}13t"


def get_window_title() -> str:
    return f"{CSI}13t"


def set_window_title(title: str) -> str:
    return f"{CSI}2;{title}t"


def report_icon_name() -> str:
    return f"{CSI}20t"


def report_window_icon() -> str:
    return f"{CSI}20t"


def reset_keyboard_mode() -> str:
    return f"{CSI}1h"


def set_keyboard_action_mode(mode: int) -> str:
    return sequence("h", mode)


def set_cursor_keys_mode(application: bool = True) -> str:
    return f"{CSI}?1{'h' if application else 'l'}"


def set_alt_screen_function_key_mode(application: bool = True) -> str:
    return f"{CSI}?27{'h' if application else 'l'}"


def set_local_modes(local: bool = True) -> str:
    return f"{CSI}?20{'h' if local else 'l'}"


def set_no_wrap(enable: bool = True) -> str:
    return f"{CSI}?7{'h' if enable else 'l'}"


def set_reverse_video(on: bool = True) -> str:
    return f"{CSI}?5{'h' if on else 'l'}"


def scroll_left(columns: int = 1) -> str:
    return sequence("@", max(1, columns))


def scroll_right(columns: int = 1) -> str:
    return sequence("@", max(1, columns))


def media_copy_top(page: int = 0) -> str:
    return sequence("i", page)


def media_copy_bottom(page: int = 0) -> str:
    return sequence("i", page + 1)
