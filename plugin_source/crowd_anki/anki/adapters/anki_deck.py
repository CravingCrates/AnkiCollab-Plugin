from dataclasses import dataclass
from typing import Callable


@dataclass
class AnkiDeck:
    _data: dict

    deck_name_separator = '::'

    @property
    def data(self):
        return self._data

    @property
    def is_dynamic(self):
        return bool(self.data['dyn'])

    @property
    def name(self):
        return self.data['name']