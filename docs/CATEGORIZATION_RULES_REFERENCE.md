# Categorization Rules Reference: Pre-ML & Post-ML

This document provides a comprehensive guide and complete reference of all deterministic rule sets used in the transaction categorization and decision pipeline, structured by their execution order: **Pre-ML Rules** and **Post-ML Fallback Rules**.

---

## 1. Execution Flow Overview

```
                          Transaction Narration
                                   │
                                   ▼
                      [Pre-ML Rule Engine]
                (rules_config.py & rule_engine.py)
                                   │
        ┌──────────────────────────┴──────────────────────────┐
        ▼                                                     ▼
High Confidence (Score >= 70)                         Low / Medium Confidence
        │                                                     │
        ▼                                                     ▼
Validate with ML / Accept                       [ML Classification Model]
                                                              │
                                            ┌─────────────────┴─────────────────┐
                                            ▼                                   ▼
                                     ML High Confidence                ML Abstains / Low Conf
                                            │                                   │
                                            ▼                                   ▼
                                        Accept ML                     [Post-ML Fallbacks]
                                                                        (_last_resort)
                                                                                │
                                                            ┌───────────────────┴───────────────────┐
                                                            ▼                                       ▼
                                                 [Narration Pattern Rules]              [Trade Name Matching]
                                                    (purpose_rules.py)                       (trades.py)
                                                            │                                       │
                                                            └───────────────────┬───────────────────┘
                                                                                │
                                                                                ▼
                                                                     [Counterparty Memory]
                                                                  (counterparty_memory.py)
                                                                                │
                                                                                ▼
                                                                     Final Result / Review Queue
```

---

## 2. Pre-ML Rules (`app/categorization/rules_config.py`)

Deterministic rules evaluated prior to ML model inference. Matches are scored and ranked so that specific, strong signals always outrank weak or ambiguous keywords.

### Priority Tiers & Scoring

| Tier | Name | Score | Purpose & Description |
| :--- | :--- | :---: | :--- |
| **Tier 0** | `INTENT_PHRASE` | 110 | Explicit statements of money movement nature (e.g. self transfer); strictly overrides merchant matches. |
| **Tier 1** | `EXACT_MERCHANT` | 100 | Exact brand/entity name found on clean word boundaries. |
| - | `MERCHANT_IN_TOKEN` | 90 | Merchant name found concatenated inside a token (`UPI-NAMMAYATRI`). |
| **Tier 2** | `STRONG_PHRASE` | 80 | Multi-word phrase unambiguous in context (e.g. `BANK CHARGES`, `SALARY CREDIT`). |
| - | `PHRASE_IN_TOKEN` | 80 | Multi-word phrase embedded within a compound token (`NEFT-TRANSACTIONCHARGE`). |
| **Tier 3** | `STRONG_KEYWORD` | 60 | Single keyword that strongly implies a category (e.g. `RESTAURANT`, `PHARMACY`). |
| **Tier 4** | `CONTEXTUAL` | 40 | Combination of tokens that are meaningful only when appearing together (e.g. `["BILL", "PAYMENT"]`). |
| **Tier 5** | `WEAK_KEYWORD` | 20 | Suggestive keyword; cannot auto-accept on its own (e.g. `UPI`, `NEFT`, `STORE`). |
| **Signal** | `AMOUNT_SIGNAL` | +10 | Direction/amount nudge (e.g. `CREDIT` > 5000 for `Salary/Income`). |

---

### Complete Pre-ML Rules Catalog

#### Tier 0: Intent Phrases (Score: 110)
* **Transfers**: `TRANSFER TO SELF`, `SELF TRANSFER`, `TRANSFER TO OWN ACCOUNT`, `OWN ACCOUNT TRANSFER`, `UPI TRANSFER`, `NEFT TRANSFER`, `RTGS TRANSFER`, `IMPS TRANSFER`, `FUND TRANSFER`, `FUNDS TRANSFER`, `TRANSFER TO`, `ACCOUNT TRANSFER`

---

#### Tier 1: Exact Merchants (Score: 100)
* **Food & Dining**: `SWIGGY`, `ZOMATO`, `MCDONALDS`, `MC DONALDS`, `KFC`, `DOMINOS`, `PIZZA HUT`, `STARBUCKS`, `SUBWAY`, `BURGER KING`, `DUNKIN`, `BARBEQUE NATION`, `HALDIRAM`, `CAFE COFFEE DAY`, `CCD`, `CHAIPOINT`, `FAASOS`, `BEHROUZ`, `OVENSTORY`, `EATFIT`, `BOX8`
* **Groceries**: `BIGBASKET`, `BIG BASKET`, `BLINKIT`, `ZEPTO`, `DMART`, `D MART`, `RELIANCE FRESH`, `RELIANCE SMART`, `MORE SUPERMARKET`, `JIOMART`, `NATURES BASKET`, `SPENCERS`, `STAR BAZAAR`, `GROFERS`, `INSTAMART`
* **Transportation**: `UBER`, `OLA`, `RAPIDO`, `NAMMA YATRI`, `BMRC`, `BANGALORE METRO`, `IRCTC`, `REDBUS`, `BLUSMART`, `BLU SMART`, `DELHI METRO`, `DMRC`, `INDIAN OIL`, `IOCL`, `BHARAT PETROLEUM`, `BPCL`, `HINDUSTAN PETROLEUM`, `HPCL`, `SHELL`, `FASTAG`, `PAYTM FASTAG`
* **Shopping**: `AMAZON`, `FLIPKART`, `MYNTRA`, `AJIO`, `MEESHO`, `CROMA`, `RELIANCE DIGITAL`, `DECATHLON`, `NYKAA`, `TATA CLIQ`, `SNAPDEAL`, `LIFESTYLE`, `PANTALOONS`, `WESTSIDE`, `SHOPPERS STOP`, `IKEA`, `H AND M`, `ZARA`, `UNIQLO`
* **Entertainment**: `NETFLIX`, `SPOTIFY`, `PRIME VIDEO`, `AMAZON PRIME`, `HOTSTAR`, `DISNEY HOTSTAR`, `YOUTUBE PREMIUM`, `BOOKMYSHOW`, `PVR`, `INOX`, `SONY LIV`, `SONYLIV`, `ZEE5`, `JIOCINEMA`, `JIO CINEMA`, `GAANA`, `WYNK`, `APPLE MUSIC`, `STEAM GAMES`
* **Utilities & Bills**: `JIO`, `RELIANCE JIO`, `AIRTEL`, `BHARTI AIRTEL`, `VODAFONE IDEA`, `VODAFONE`, `BSNL`, `BESCOM`, `BWSSB`, `TATA POWER`, `ADANI ELECTRICITY`, `ACT BROADBAND`, `ACT FIBERNET`, `JIOFIBER`, `JIO FIBER`, `AIRTEL XSTREAM`, `INDANE GAS`, `HP GAS`, `BHARAT GAS`, `MSEB`, `TNEB`, `KSEB`, `TORRENT POWER`, `MAHANAGAR GAS`
* **Healthcare**: `APOLLO`, `MANIPAL`, `FORTIS`, `TATA 1MG`, `1MG`, `PHARMEASY`, `NETMEDS`, `MEDPLUS`, `PRACTO`, `THYROCARE`, `DR LAL PATHLABS`, `MAX HEALTHCARE`, `NARAYANA HEALTH`, `CLOUDNINE`
* **Education**: `COURSERA`, `UDEMY`, `UNACADEMY`, `UPGRAD`, `BYJUS`, `VEDANTU`, `SIMPLILEARN`, `GREAT LEARNING`, `SCALER`, `EDX`, `KHAN ACADEMY`
* **Travel**: `AIR INDIA`, `INDIGO`, `EMIRATES`, `BOOKING.COM`, `BOOKINGCOM`, `AIRBNB`, `OYO`, `MAKEMYTRIP`, `GOIBIBO`, `CLEARTRIP`, `YATRA`, `VISTARA`, `SPICEJET`, `AKASA AIR`, `TRIVAGO`, `AGODA`, `TAJ HOTELS`, `MARRIOTT`, `RADISSON`
* **Investments**: `ZERODHA`, `GROWW`, `UPSTOX`, `ANGEL ONE`, `ANGELONE`, `KUVERA`, `COIN ZERODHA`, `SMALLCASE`, `ICICI DIRECT`, `HDFC SECURITIES`, `KOTAK SECURITIES`, `PAYTM MONEY`, `NSE`, `BSE`

---

#### Tier 2: Strong Multi-Word Phrases (Score: 80)
* **Bank Charges**: `BANK CHARGES`, `BANK CHARGE`, `SERVICE CHARGE`, `SERVICE CHARGES`, `ANNUAL MAINTENANCE FEE`, `AMC FEE`, `ATM FEE`, `ATM CHARGES`, `CASH WITHDRAWAL FEE`, `SMS CHARGES`, `SMS CHARGE`, `TRANSACTION FEE`, `TRANSACTION CHARGE`, `TRANSACTION CHARGES`, `GST ON CHARGES`, `PROCESSING FEE`, `LATE PAYMENT FEE`, `PENALTY CHARGES`, `MIN BALANCE CHARGES`, `MINIMUM BALANCE CHARGES`, `CHEQUE RETURN CHARGES`, `NON MAINTENANCE CHARGES`, `DEBIT CARD FEE`, `CARD ANNUAL FEE`, `OVERDRAFT FEE`, `IMPS CHARGES`, `NEFT CHARGES`, `RTGS CHARGES`
* **Salary & Income**: `SALARY CREDIT`, `MONTHLY SALARY`, `PAYROLL CREDIT`, `PAYROLL`, `WAGES CREDIT`, `PERFORMANCE BONUS`, `INTEREST CREDIT`, `SALARY FOR`, `SAL CREDIT`, `STIPEND CREDIT`, `PENSION CREDIT`, `INT CR`, `INTEREST EARNED`, `DIVIDEND CREDIT`, `ANNUAL BONUS`
* **Rent & Housing**: `HOUSE RENT`, `MONTHLY RENT`, `RENT PAYMENT`, `LANDLORD PAYMENT`, `RENT TO OWNER`, `APARTMENT MAINTENANCE`, `FLAT MAINTENANCE`, `SOCIETY MAINTENANCE`, `HOME MAINTENANCE`, `RENT FOR`, `HOUSING SOCIETY`, `MAINTENANCE CHARGES SOCIETY`
* **Investments**: `MUTUAL FUND`, `MF INVESTMENT`, `SIP INVESTMENT`, `SIP DEBIT`, `SYSTEMATIC INVESTMENT`, `EQUITY PURCHASE`, `STOCK PURCHASE`, `DEMAT DEBIT`, `NPS CONTRIBUTION`, `PPF DEPOSIT`, `RD INSTALLMENT`, `FD BOOKING`, `FIXED DEPOSIT`
* **Utilities & Bills**: `MOBILE RECHARGE`, `ELECTRICITY BILL`, `WATER BILL`, `GAS BILL`, `BROADBAND BILL`, `POSTPAID BILL`, `PREPAID RECHARGE`, `DTH RECHARGE`, `INTERNET BILL`, `UTILITY PAYMENT`
* **Transportation**: `FASTAG RECHARGE`, `TOLL PLAZA`, `FUEL PURCHASE`, `PETROL PUMP`, `METRO RECHARGE`, `CAB RIDE`, `AUTO RIDE`
* **Healthcare**: `MEDICAL STORE`, `HEALTH INSURANCE`, `DIAGNOSTIC CENTRE`, `DIAGNOSTIC CENTER`, `PATH LAB`, `MEDICAL BILL`
* **Education**: `TUITION FEE`, `TUITION FEES`, `COLLEGE FEE`, `SCHOOL FEE`, `EXAM FEE`, `COURSE FEE`, `SEMESTER FEE`, `ADMISSION FEE`
* **Travel**: `FLIGHT BOOKING`, `HOTEL BOOKING`, `TRAVEL BOOKING`, `AIR TICKET`, `TICKET BOOKING`
* **Food & Dining**: `FOOD ORDER`, `FOOD DELIVERY`, `ONLINE FOOD`
* **Groceries**: `GROCERY STORE`, `GROCERY SHOPPING`, `SUPER MARKET`

---

#### Tier 3: Strong Single Keywords (Score: 60)
* **Food & Dining**: `RESTAURANT`, `CAFE`, `COFFEE`, `DINING`, `BAKERY`, `BIRYANI`, `PIZZA`, `BURGER`, `CANTEEN`, `DHABA`, `EATERY`, `BISTRO`
* **Groceries**: `GROCERY`, `GROCERIES`, `SUPERMARKET`, `VEGETABLES`, `PROVISION`, `KIRANA`
* **Transportation**: `TAXI`, `METRO`, `TOLL`, `FASTAG`, `PETROL`, `DIESEL`, `PARKING`, `CAB`
* **Shopping**: `ELECTRONICS`, `CLOTHING`, `APPAREL`, `FASHION`, `FOOTWEAR`, `FURNITURE`
* **Entertainment**: `MOVIE`, `CINEMA`, `MULTIPLEX`, `GAMING`, `ENTERTAINMENT`
* **Utilities & Bills**: `ELECTRICITY`, `BROADBAND`, `POSTPAID`, `PREPAID`, `RECHARGE`, `DTH`, `LANDLINE`
* **Healthcare**: `HOSPITAL`, `DOCTOR`, `CLINIC`, `PHARMACY`, `MEDICINE`, `MEDICAL`, `DIAGNOSTIC`, `PATHOLOGY`, `DENTAL`
* **Education**: `COLLEGE`, `UNIVERSITY`, `TUITION`, `SEMESTER`, `COACHING`
* **Travel**: `FLIGHT`, `AIRLINE`, `RESORT`, `AIRPORT`, `AIRWAYS`
* **Rent & Housing**: `LANDLORD`, `TENANT`
* **Investments**: `ZERODHA`, `BROKERAGE`, `EQUITY`, `DEMAT`, `MUTUALFUND`

---

#### Tier 4 & 5: Contextual Combinations & Weak Keywords
* **Contextual (Score: 40)**:
  * `Rent & Housing`: `["RENT"]`
  * `Salary & Income`: `["SALARY"]`, `["BONUS", "CREDIT"]`, `["INTEREST", "CREDIT"]`
  * `Investments`: `["SIP"]`, `["STOCK"]`
  * `Utilities & Bills`: `["BILL", "PAYMENT"]`, `["GAS"]`, `["WATER"]`
  * `Transportation`: `["FUEL"]`, `["BUS"]`, `["TRAIN"]`
  * `Education`: `["COURSE"]`, `["EXAM"]`, `["SCHOOL"]`
  * `Travel`: `["HOTEL"]`, `["TRAVEL"]`, `["BOOKING"]`
  * `Shopping`: `["SHOPPING"]`, `["SHOES"]`
  * `Entertainment`: `["SUBSCRIPTION"]`, `["MUSIC"]`
  * `Healthcare`: `["HEALTH"]`, `["LAB"]`
  * `Food & Dining`: `["FOOD"]`, `["MESS"]`
  * `Groceries`: `["FRUITS"]`, `["MILK"]`
* **Weak Keywords (Score: 20)**:
  * `Transfers`: `NEFT`, `RTGS`, `IMPS`, `UPI`, `ACH`, `ECS`
  * `Shopping`: `STORE`, `MART`, `RETAIL`, `MALL`
  * `Food & Dining`: `KITCHEN`, `JUICE`, `SNACK`

---

## 3. Post-ML Fallback Rules (`_last_resort`)

When ML confidence is low/unaccepted or rules are inconclusive, `_last_resort()` runs secondary fallbacks.

### A. Narration Pattern & Purpose Rules (`app/categorization/purpose_rules.py`)

Handles OCR-mangled text, bank charges, statutory items, and payment rails.

| Pattern / Match | Purpose (Category) | Event Type | Note | Certainty |
| :--- | :--- | :--- | :--- | :---: |
| `PHONEPE`, `PAYTM`, `RAZORPAY`, `CASHFREE`, `SODEXO`, `EAZYDINER`, `SWIGGY`, `ZOMATO`, `DINEOUT`, `MAGICPIN`, `BT<digits>/` | Sales Income | Merchant Settlement | Payment gateway / aggregator settlement | Provisional |
| `PLUXEE`, `BOBCARD`, `BHARATPE`, `PINELABS`, `MSWIPE`, `EZETAP`, `WORLDLINE`, `BILLDESK`, `CCAVENUE`, `INSTAMOJO`, `JUSPAY` | Sales Income | Merchant Settlement | Card acquirer / meal card settlement | **Certain** |
| `POSRENT`, `POS RENT` | Bank Fees | Bank Charge | POS terminal rental | **Certain** |
| `CBDT`, `TIN<digits>`, `GST`, `TDS`, `ESIC`, `EPFO`, `PF` | Taxes & Statutory | Statutory Payment | Direct tax / GST / TDS / statutory challan | **Certain** |
| `RENT` + `GST` | Rent (Premises) | Vendor Payment | Rent with GST invoice | Provisional |
| `INT.COLL`, `PENAL CHARGE/INT`, `INTEREST CHARGED/DEBIT` | Finance Cost | Bank Charge | Loan / Overdraft interest debited by bank | **Certain** |
| `CHARGES FOR`, `PROCESSING FEE/CHG`, `NEFT/IMPS CHG`, `SMS CHARGE`, `LEDGER FOLIO`, `CASH HANDLING`, `CHEQUE BOOK` | Bank Fees | Bank Charge | Bank service & operational fees | **Certain** |
| `BY CASH`, `TO CASH`, `CASH DEP`, `ATM WDL`, `SELF` | Internal Movement | Internal Transfer | Cash deposit/withdrawal, self account transfer | **Certain** |

---

### B. Counterparty Trade Name Matching (`app/categorization/trades.py`)

Extracts trade descriptors from the counterparty's business name when the narration does not have a high-confidence rule:

| Business Sector | Flat Purpose | Path (Personal / Business) | Trigger Keywords Matched |
| :--- | :--- | :--- | :--- |
| **Food, Meat & Poultry** | Cost of Goods | Food & Dining > Groceries / Business > Inventory | `FISH`, `SEAFOOD`, `PRAWNS`, `CRAB`, `MEAT`, `MUTTON`, `BEEF`, `PORK`, `CHICKEN`, `POULTRY`, `EGGS` |
| **Vegetables & Fruits** | Cost of Goods | Food & Dining > Groceries / Business > Inventory | `VEG`, `VEGETABLES`, `GREENS`, `FRUITS`, `SABZI`, `SUBZI`, `MANDI` |
| **Dairy** | Cost of Goods | Food & Dining > Groceries / Business > Inventory | `MILK`, `DAIRY`, `CURD`, `PANEER`, `GHEE`, `AMUL`, `NANDINI`, `HERITAGE FOODS`, `AAVIN` |
| **Staples & Spices** | Cost of Goods | Food & Dining > Groceries / Business > Raw Materials | `RICE`, `WHEAT`, `ATTA`, `FLOUR`, `DAL`, `PULSES`, `GRAINS`, `MASALA`, `SPICES`, `OIL MILLS`, `EDIBLE OIL` |
| **Bakery & Sweets** | Cost of Goods | Food & Dining > Cafes / Business > Inventory | `BAKERY`, `BAKERS`, `SWEETS`, `SWEET HOUSE`, `CONFECTIONERY`, `NAMKEEN`, `SNACKS` |
| **Provisions & Kirana**| Cost of Goods | Food & Dining > Groceries / Business > Inventory | `KIRANA`, `GROCERY`, `PROVISIONS`, `SUPERMARKET`, `HYPERMARKET`, `GENERAL STORES`, `DEPARTMENTAL` |
| **Food Businesses** | Cost of Goods | Food & Dining > Restaurants / Business > Inventory | `HOTEL`, `RESTAURANT`, `DHABA`, `MESS`, `TIFFIN`, `CANTEEN`, `CATERERS`, `FOOD COURT`, `CAFE` |
| **Beverages** | Cost of Goods | Food & Dining > Cafes / Business > Inventory | `BEVERAGES`, `BREWERY`, `DISTILLERY`, `WINES`, `LIQUOR`, `SOFT DRINKS` |
| **Fuel & Petrol** | Transportation | Transportation > Fuel / Transportation > Fuel | `PETROL BUNK`, `FUEL STATION`, `FILLING STATION`, `IOCL`, `BPCL`, `HPCL`, `NAYARA`, `GAS AGENCY` |
| **Automotive & Garages** | Transportation | Transportation > Vehicle Expenses | `MARUTI`, `HYUNDAI`, `TOYOTA`, `MAHINDRA`, `TATA MOTORS`, `GARAGE`, `TYRES`, `SPARE PARTS` |
| **Logistics & Freight** | Cost of Goods | Transportation > Other / Business > Supplier Payment | `TRANSPORTS`, `LOGISTICS`, `ROADWAYS`, `CARRIERS`, `CARGO`, `COURIERS`, `FREIGHT`, `PACKERS & MOVERS` |
| **Travel Agencies** | Other Purpose | Travel > Travel Agencies / Business > Business Travel | `TRAVELS`, `TOURS & TRAVELS`, `TRAVEL AGENCY` |
| **Healthcare & Pharma** | Other Purpose | Healthcare > Pharmacy | `PHARMACY`, `MEDICALS`, `CHEMISTS`, `HOSPITALS`, `CLINICS`, `DIAGNOSTICS`, `LABS`, `SCAN CENTRE` |
| **Hardware & Building**| Cost of Goods | Housing > Home Improvement / Business > Raw Materials | `CEMENT`, `STEEL`, `IRON`, `TMT`, `HARDWARE`, `TIMBER`, `PLYWOOD`, `TILES`, `MARBLE`, `PAINTS`, `BUILDERS` |
| **Textiles & Garments** | Cost of Goods | Shopping > Clothing / Business > Inventory | `TEXTILES`, `GARMENTS`, `FABRICS`, `SILKS`, `COTTONS`, `SAREES`, `APPARELS`, `TAILORS` |
| **Packaging & Plastics**| Cost of Goods | Shopping > Other / Business > Raw Materials | `PACKAGING`, `CARTONS`, `CORRUGATED`, `PLASTICS`, `POLYMERS`, `POUCH` |
| **Printing & Stationery**| Cost of Goods | Shopping > Other / Business > Office Expenses | `PRINTERS`, `PRINTING`, `PRESS`, `STATIONERY`, `XEROX`, `GRAPHICS`, `SIGNAGES` |
| **Electricals** | Cost of Goods | Shopping > Electronics / Business > Inventory | `ELECTRICALS`, `ELECTRONICS`, `CABLES`, `WIRES`, `LIGHTINGS`, `APPLIANCES`, `HVAC` |
| **Utilities** | Utilities | Bills & Utilities | `POWER CORP`, `DISCOM`, `ELECTRICITY BOARD`, `WATER BOARD`, `MUNICIPAL CORPORATION` |
| **Legal & Accounting** | Professional Fees | Business > Professional Services | `CHARTERED ACCOUNTANTS`, `AUDITORS`, `ADVOCATES`, `LAWYERS`, `LEGAL ASSOCIATES`, `LAW FIRM` |
| **Tech & Consulting** | Professional Fees | Business > Professional Services | `CONSULTANTS`, `ADVISORY`, `TECHNOLOGY`, `SOFTWARE`, `INFOTECH`, `IT SERVICES`, `DIGITAL`, `WEB SERVICES` |
| **Facilities & Security**| Cost of Goods | Business > Office Expenses | `SECURITY SERVICES`, `MANPOWER`, `FACILITY MANAGEMENT`, `HOUSEKEEPING`, `PEST CONTROL`, `CLEANING` |
| **Marketing & Media** | Professional Fees | Business > Marketing & Advertising | `ADVERTISING`, `MARKETING`, `MEDIA`, `BRANDING`, `CREATIVES`, `STUDIOS` |
| **Education** | Other Purpose | Education > Tuition & Fees | `SCHOOLS`, `COLLEGES`, `UNIVERSITIES`, `ACADEMY`, `INSTITUTES`, `TUITIONS`, `COACHING` |
| **Agriculture & Agro** | Cost of Goods | Food & Dining > Groceries / Business > Raw Materials | `AGRO`, `AGRI`, `SEEDS`, `FERTILIZERS`, `PESTICIDES`, `NURSERY`, `FARMS`, `PLANTATIONS` |

---

### C. Counterparty Memory Overrides (`app/categorization/counterparty_memory.py`)

* **Rule**: When a reviewer corrects or explicitly assigns a category to a counterparty, the choice is persisted in `counterparty_memory`.
* **Execution**: During subsequent runs, `memory_should_override` checks if the previous rule came from a trade-name match or unconfident classifier. If so, the reviewer's saved choice overrides the default matching.
