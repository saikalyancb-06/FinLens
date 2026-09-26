// format.js - Currency and Number Formatting Utilities for Kredo ERP Design System

(function (window) {
    // Calculates dynamic font size based on text length to fit single-line containers
    function calcStatFontSize(text) {
        const str = String(text || '').trim();
        const len = str.length;
        if (len > 18) return '1.05rem';
        if (len > 15) return '1.18rem';
        if (len > 13) return '1.32rem';
        if (len > 10) return '1.5rem';
        if (len > 7) return '1.68rem';
        return '1.9rem';
    }

    const Format = {
        // Formats amount into full Indian currency representation e.g. ₹1,25,40,000.00
        money: function (amount, options = {}) {
            if (amount === undefined || amount === null || isNaN(amount)) {
                return '₹0.00';
            }
            const num = Number(amount);
            const isNegative = num < 0;
            const absVal = Math.abs(num);
            
            const fixedStr = absVal.toFixed(2);
            const parts = fixedStr.split('.');
            let integerPart = parts[0];
            const decimalPart = parts[1];
            
            let lastThree = integerPart.substring(integerPart.length - 3);
            let otherDigits = integerPart.substring(0, integerPart.length - 3);
            if (otherDigits !== '') {
                lastThree = ',' + lastThree;
            }
            const formattedInt = otherDigits.replace(/\B(?=(\d{2})+(?!\d))/g, ",") + lastThree;
            const formattedStr = (isNegative ? '₹-' : '₹') + formattedInt + '.' + decimalPart;

            return formattedStr;
        },

        moneyShort: function (amount, options = {}) {
            if (amount === undefined || amount === null || isNaN(amount)) {
                return '₹0.00';
            }
            const num = Number(amount);
            const isNegative = num < 0;
            const absVal = Math.abs(num);
            
            const fixedStr = absVal.toFixed(2);
            const parts = fixedStr.split('.');
            let integerPart = parts[0];
            const decimalPart = parts[1];
            
            let lastThree = integerPart.substring(integerPart.length - 3);
            let otherDigits = integerPart.substring(0, integerPart.length - 3);
            if (otherDigits !== '') {
                lastThree = ',' + lastThree;
            }
            const formattedInt = otherDigits.replace(/\B(?=(\d{2})+(?!\d))/g, ",") + lastThree;
            const formattedStr = (isNegative ? '₹-' : '₹') + formattedInt + '.' + decimalPart;

            if (options.html) {
                const fontSize = calcStatFontSize(formattedStr);
                const customStyle = options.style || '';
                return `<span style="font-size: ${fontSize}; white-space: nowrap; word-break: keep-all; ${customStyle}">${formattedStr}</span>`;
            }
            return formattedStr;
        },

        // Formats an amount in an arbitrary currency.
        //
        // Grouping is not cosmetic. The lakh/crore grouping above is correct for
        // rupees and wrong for everything else: a dollar figure written
        // $12,34,567.89 reads as a typo to the people who need it. Indian
        // grouping is therefore used for INR only, and Western 3-digit grouping
        // for the rest.
        //
        // Decimals come from the currency, not a constant: yen has none, so
        // rendering "¥172,414.00" invents a precision the currency does not have.
        moneyCcy: function (amount, opts = {}) {
            const symbol = opts.symbol !== undefined ? opts.symbol : '\u20B9';
            const code = opts.code || 'INR';
            const decimals = opts.decimals !== undefined && opts.decimals !== null
                ? opts.decimals : 2;
            if (amount === undefined || amount === null || isNaN(amount)) {
                return symbol + (decimals > 0 ? '0.' + '0'.repeat(decimals) : '0');
            }
            const num = Number(amount);
            const isNegative = num < 0;
            const fixedStr = Math.abs(num).toFixed(decimals);
            const parts = fixedStr.split('.');
            let integerPart = parts[0];
            const decimalPart = parts[1];

            let formattedInt;
            if (code === 'INR') {
                let lastThree = integerPart.substring(integerPart.length - 3);
                let otherDigits = integerPart.substring(0, integerPart.length - 3);
                if (otherDigits !== '') lastThree = ',' + lastThree;
                formattedInt = otherDigits.replace(/\B(?=(\d{2})+(?!\d))/g, ",") + lastThree;
            } else {
                formattedInt = integerPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
            }

            return symbol + (isNegative ? '-' : '') + formattedInt
                 + (decimalPart ? '.' + decimalPart : '');
        },

        // Rescale an amount into a chosen unit for display.
        //
        // A treasury dashboard reading "1,24,53,67,891.00" in every tile is
        // technically correct and unreadable. Scaling is presentation only —
        // the underlying figure is untouched, and the unit is always printed
        // next to the number, because "12.45" means nothing without it.
        //
        // Indian units (lakh = 10^5, crore = 10^7) sit alongside the Western
        // ones deliberately: this is an Indian treasury product, and a CFO here
        // thinks in crores, not millions.
        DENOMINATIONS: {
            units:    { label: 'Units',     suffix: '',   divisor: 1 },
            thousands:{ label: 'Thousands', suffix: 'K',  divisor: 1e3 },
            lakhs:    { label: 'Lakhs',     suffix: 'L',  divisor: 1e5 },
            millions: { label: 'Millions',  suffix: 'M',  divisor: 1e6 },
            crores:   { label: 'Crores',    suffix: 'Cr', divisor: 1e7 },
        },

        // Formats an amount scaled to `denom`, e.g. moneyScaled(12453678, 'crores')
        // -> "₹1.25 Cr". Falls back to the plain formatter for 'units'.
        moneyScaled: function (amount, denom, opts = {}) {
            const d = Format.DENOMINATIONS[denom] || Format.DENOMINATIONS.units;
            const symbol = opts.symbol !== undefined ? opts.symbol : '₹';
            const code = opts.code || 'INR';
            // Honoured at UNIT scale, deliberately ignored above it — the two
            // cases ask different questions.
            //
            // At unit scale this is moneyCcy with an empty suffix, so it has to
            // BEHAVE like moneyCcy, including the rule two functions up that
            // decimals come from the CURRENCY. Forcing 2 here is what stopped
            // the Transactions grid using this at all: it would have rendered
            // yen as "¥172,414.00", inventing a precision the currency does
            // not have.
            const unitDecimals = opts.decimals !== undefined && opts.decimals !== null
                ? opts.decimals : 2;
            if (amount === undefined || amount === null || isNaN(amount)) {
                return d.divisor === 1
                    ? Format.moneyCcy(amount, { symbol: symbol, code: code, decimals: unitDecimals })
                    : symbol + '0' + (d.suffix ? ' ' + d.suffix : '');
            }
            if (d.divisor === 1) {
                return Format.moneyCcy(amount, { symbol: symbol, code: code, decimals: unitDecimals });
            }
            const scaled = Number(amount) / d.divisor;
            // Two decimals ONCE SCALED, whatever the currency. These no longer
            // describe minor units — they are the only thing separating 1.4 Cr
            // from 1.6 Cr, and dropping them would hide 20 million rupees.
            const body = Format.moneyCcy(scaled, { symbol: symbol, code: code, decimals: 2 });
            return body + ' ' + d.suffix;
        },

        // Auto-scaled money for tiles, axis ticks and bar labels.
        //
        // The dashboard used to print "₹4,48,60,718.25" in a KPI tile. That is the
        // right number and the wrong unit: nobody scans eight digits, and every
        // tile beside it was equally unscannable, so nothing could be compared
        // at a glance. This picks the unit from the magnitude instead - crore,
        // lakh, thousand for rupees; the M/K ladder everyone else uses for other
        // currencies, because "₹4.49 Cr" is right and "$4.49 Cr" is not.
        //
        // Presentation only. The exact figure is still what the tooltips and
        // tables print, and nothing stored is touched.
        moneyCompact: function (amount, opts = {}) {
            const symbol = opts.symbol !== undefined ? opts.symbol : '₹';
            const code = opts.code || 'INR';
            if (amount === undefined || amount === null || isNaN(amount)) return '--';

            const num = Number(amount);
            const abs = Math.abs(num);
            const ladder = code === 'INR'
                ? [[1e7, 'Cr'], [1e5, 'L'], [1e3, 'K']]
                : [[1e9, 'B'], [1e6, 'M'], [1e3, 'K']];

            for (const [divisor, suffix] of ladder) {
                if (abs >= divisor) {
                    const scaled = num / divisor;
                    // Two decimals below 100 units, one above: "₹1.25 Cr" needs the
                    // precision, "₹124.5 Cr" does not, and both fit a tile.
                    const decimals = Math.abs(scaled) >= 100 ? 1 : 2;
                    // A round figure loses its decimals: "₹50 K" is the number,
                    // "₹50.00 K" is the number wearing two digits of false
                    // precision. Anything that is not round keeps them.
                    return Format.moneyCcy(scaled, { symbol, code, decimals })
                             .replace(/\.0+$/, '') + ' ' + suffix;
                }
            }
            // Small change keeps its paise; a bank charge of ₹26.66 is not "₹0 K".
            return Format.moneyCcy(num, { symbol, code, decimals: abs < 1000 ? 2 : 0 });
        },

        number: function (val) {
            if (val === undefined || val === null || isNaN(val)) return '0';
            return Number(val).toLocaleString('en-IN');
        }
    };

    window.Format = Format;
    window.money = Format.money;
    window.moneyShort = Format.moneyShort;
    window.moneyCcy = Format.moneyCcy;
    window.moneyScaled = Format.moneyScaled;
    window.moneyCompact = Format.moneyCompact;
    window.DENOMINATIONS = Format.DENOMINATIONS;
    window.calcStatFontSize = calcStatFontSize;
})(window);
