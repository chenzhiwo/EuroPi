# Copyright 2024 Allen Synthesis
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pytest
import ssd1306

from europi_display import Display


def test_show_page_selects_one_page_and_reuses_preallocated_view():
    display = Display(128, 32, 0, 1, 0, 400_000, 255, False)
    commands = []
    writes = []
    display.write_cmd = commands.append
    display.write_data = writes.append

    display.show_page(2)
    first_view = writes[0]
    display.show_page(2)

    assert commands[:6] == [
        ssd1306.SET_COL_ADDR,
        0,
        127,
        ssd1306.SET_PAGE_ADDR,
        2,
        2,
    ]
    assert len(first_view) == 128
    assert writes[1] is first_view


def test_show_page_rejects_out_of_range_page():
    display = Display(128, 32, 0, 1, 0, 400_000, 255, False)

    with pytest.raises(ValueError):
        display.show_page(4)
