# Notices and data attribution

MTG Tracker republishes data derived from the sources below. Where each number
comes from, and how it is computed, is in [`DATA-SOURCES.md`](DATA-SOURCES.md).

## MTGJSON

`cards_index.json`, the crack values in `prices.json` (`crack`), the box
expected values (`boxes[][].ev`) and the bundle contents checks are derived
from [MTGJSON](https://mtgjson.com/) data, used under the MIT License:

```
MIT License

Copyright © 2018-Present Zachary Halpern

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Source: [mtgjson/mtgjson LICENSE.txt](https://github.com/mtgjson/mtgjson/blob/master/LICENSE.txt)
(also at [mtgjson.com/license](https://mtgjson.com/license/)).

## Price and score sources

- **TCGplayer** prices, read through [TCGCSV](https://tcgcsv.com/), a free
  public mirror of TCGplayer's catalog and prices.
- **Card Kingdom** prices, stock and buylist, from Card Kingdom's public
  [sealed pricelist](https://api.cardkingdom.com/api/sealed_pricelist).
- **ScryCheck** power scores and brackets, from
  [scrycheck.com](https://scrycheck.com/precons). They are automated
  estimates; see ScryCheck's [accuracy notes](https://scrycheck.com/about).

## Wizards of the Coast

MTG Tracker is unofficial Fan Content permitted under the
[Fan Content Policy](https://company.wizards.com/en/legal/fancontentpolicy).
Not approved/endorsed by Wizards. Portions of the materials used are property
of Wizards of the Coast. ©Wizards of the Coast LLC.
