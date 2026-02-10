# Backend Requirements: Smart Plan - Demand Planning & Inventory Management System

## Context

Smart Plan is a comprehensive demand planning and inventory management platform for retail/distribution businesses. The system helps demand planners:
- Monitor sales performance and inventory health across multiple branches/stores
- Create and manage demand forecasting reports
- Track product assortments and pricing across locations
- Manage supply chain orders and distribution
- Analyze inventory health and identify issues

**Primary users**: Demand planners, supply chain managers, inventory analysts

**Core value**: Centralized visibility into inventory, sales forecasts, and distribution to optimize stock levels and reduce waste.

---

## Screens/Components

### 1. Login Screen
**Purpose**: Authenticate users to access the planning system

**Data I need to display**:
- The application name/branding (already in frontend)

**Actions**:
- User submits login credentials (username/email + password) → User is authenticated and redirected to dashboard
- Login fails → Show error message indicating invalid credentials

**States to handle**:
- **Loading**: While authentication is in progress
- **Error**: Invalid credentials, network error, account locked/disabled
- **Success**: Redirect to dashboard

**Business rules affecting UI**:
- Not sure if there are role-based permissions (demand planner vs. admin vs. analyst)
- Should there be password reset functionality?
- Are there account lockout rules after failed attempts?

---

### 2. Dashboard (Overview)
**Purpose**: High-level KPI overview showing business health across sales, forecasts, and inventory

**Data I need to display**:
- **Sales metrics**:
  - Total sales value for selected period
  - Comparison to previous period or plan (percentage indicator)
- **Forecast accuracy metrics**:
  - Multiple forecast quality indicators (e.g., "Индекс здоровья товарного запаса")
  - Shows surplus/deficit percentages for different categories (A, B, C)
  - Each category shows: total sales value, inventory stock percentage, health index score
- **Time-series chart data**:
  - Historical actuals (fact line)
  - Baseline forecast (dotted line)
  - Target/goal (another line)
  - Corrected forecast (dotted line)
  - Needs data points by time period (appears to be monthly: Jul, Aug, Sep, Oct, Nov, Dec, Jan, Feb, Mar, Apr, May)
- **Inventory coverage visualization**:
  - Shows proportion of low/medium/healthy stock levels
  - Percentage breakdown by health status

**Actions**:
- User selects date range filter → Dashboard metrics recalculate for that period
- User switches between views (ДСП / Короб toggles) → Metrics switch between units/boxes
- User clicks navigation to drill into specific areas → Navigate to detail page

**States to handle**:
- **Loading**: While fetching dashboard data
- **Empty**: No data for selected period (unlikely for dashboard, but possible for new accounts)
- **Error**: Failed to load metrics or charts
- **Partial**: Some KPIs load but charts fail (should I show what's available?)

**Business rules affecting UI**:
- What determines the "health" threshold percentages (e.g., 70% = good, 78% = warning)?
- Are the category labels (A, B, C) configurable or fixed?
- Can users customize which KPIs appear on their dashboard?
- Should date ranges be restricted (e.g., only last 2 years of data)?

---

### 3. DP Report (Demand Planning Report) - List View (`/reports/list`)
**Purpose**: Browse existing demand planning reports or create new ones

**Data I need to display**:
- List of saved reports, each showing:
  - Report name/title
  - Report type or category (if applicable)
  - Creation date or last modified date
  - Preview thumbnail or icon
- "Create new report" card/button prominently displayed

**Actions**:
- User clicks "Create new report" → Navigate to new report form
- User clicks existing report → Navigate to that report's detail view
- User deletes a report (if allowed) → Confirm and remove from list

**States to handle**:
- **Empty**: No reports yet created (show prominent "Create new report" prompt)
- **Loading**: Fetching report list
- **Error**: Failed to load reports

**Business rules affecting UI**:
- Can users edit/delete existing reports, or are they immutable once created?
- Are reports owned by individual users or shared across team?
- Should there be filtering/sorting (by date, name, type)?

---

### 4. DP Report - Create New (`/reports/new`)
**Purpose**: Guide user through creating a new demand planning report

**Data I need to display**:
- Same time-series chart setup as dashboard (actuals, forecasts, targets)
- Available filter options:
  - Product selection (dropdown showing "Все" = all)
  - Warehouse/location selection (dropdown showing "Все" = all)
  - Calculation view toggle (ДСП / Короб / Чистый Вес)
  - Date range picker (currently showing "Янв 2025 - Дек 2025")
- Report creation intro screen:
  - Hero image (team photo)
  - Motivational text
  - "Let's Go" button to begin

**Actions**:
- User clicks "Let's Go" → Proceed to report configuration
- User selects filters and parameters → Preview updates
- User clicks "Редактировать" (Edit) → Modify report settings
- User clicks "Сохранить" (Save) → Save report and return to list or detail view

**States to handle**:
- **Loading**: While generating report preview
- **Empty**: No data for selected filters
- **Error**: Report generation failed

**Business rules affecting UI**:
- What validation is needed before saving a report?
- Can reports be saved as drafts vs. finalized?
- Are there templates or presets users can start from?

---

### 5. DP Report - Detail View (`/reports/1`, `/reports/2`)
**Purpose**: View detailed demand planning analysis for a specific report

**Data I need to display**:
- Report title/ID (e.g., "№1 Отчет по планированию спроса", "№2 Отчет по планированию спроса")
- Same filter controls as create view:
  - Product filter
  - Warehouse filter
  - View type (ДСП / Короб / Чистый Вес)
  - Date range
- Time-series chart with multiple forecast lines
- Detailed data table below chart showing:
  - Time periods as columns (monthly: Июль 10, Авг 10, Сент 10, etc.)
  - Rows for different metrics:
    - Факт (Actual)
    - Цель (Target)
    - Прогноз (базовый) (Baseline forecast)
    - Прогноз (скорректированный) (Corrected forecast)
    - Доступный товар (Available inventory)
  - Numeric values for each cell

**Actions**:
- User adjusts filters → Chart and table recalculate
- User clicks "Редактировать" → Enter edit mode for report
- User clicks "Сохранить" → Save changes to report
- User exports data (not visible in screenshots, but likely needed)

**States to handle**:
- **Loading**: While fetching report data
- **Error**: Report not found or failed to load
- **Partial**: Chart loads but table fails (or vice versa)

**Business rules affecting UI**:
- Can users edit historical actuals or only forecasts?
- What's the relationship between baseline and corrected forecasts?
- Are there approval workflows for reports?

---

### 6. Assortment Management (`/assortment`)
**Purpose**: Manage product assortments, pricing, and assignments across locations

**Data I need to display**:
Three tab views:

**Tab 1: "Весь ассортимент" (All Assortment)**
- Table with columns:
  - Product SKU code
  - Product name
  - Base SKU reference
  - Quantity in boxes
  - Volume (м3)
  - Status indicator (Active, New, TBD, Disc, Not filled)
  - Each status has a colored dot (green, blue, yellow, gray, red)
- Instruction text: "Чтобы начать, заполните статусы по каждому товару и подразделению"
- Completion indicator: "Заполнено на 0%"

**Tab 2: "Ассортиментная матрица по филиалам" (Assortment Matrix by Branches)**
- Same product rows as Tab 1
- Multiple branch columns (Филиалы) showing "Норма дней" (days norm)
- Column headers show branch names: Data 4, Data 5, Data 6, Data 7, etc.
- Numeric values in cells (possibly day counts or targets)

**Tab 3: "Прайс-лист" (Price List)**
- Product SKU and name columns
- Date column
- Index-Price column
- Price-list column
- Numeric values for pricing data

**Actions**:
- User switches between tabs → Load respective view
- User updates status for product → Status changes and completion % updates
- User edits cell values in matrix/price list → Values update
- User clicks "Загрузить Excel" → Open modal with upload options:
  - "Таблица с ассортиментом"
  - "Таблица по филиалам"
  - "Таблица по продажам"
  - "Таблица по Прайс-листу"
- User clicks "Сохранить" → Save all changes

**States to handle**:
- **Loading**: While fetching assortment data
- **Empty**: No products in assortment yet
- **Error**: Failed to load or save
- **Uploading**: Excel file being processed

**Business rules affecting UI**:
- Who determines which products are "Active" vs "Disc" (discontinued)?
- How does the completion percentage calculation work?
- Can users bulk-update statuses?
- What validation happens on Excel uploads?

---

### 7. Supply Chain / Orders (`/supply-chain`)
**Purpose**: View and manage supply orders with forecasting data

**Data I need to display**:
- Summary cards at top:
  - Общая сумма (Total amount): Dollar value
  - Общее кол-во коробок (Total boxes): Count
  - Общий Вес Брутто (Total gross weight): Weight
  - Общий Объем м3 (Total volume): Volume in cubic meters
- Filter controls:
  - Category filter (dropdown showing "Все")
  - Source filter (dropdown showing "Все")
  - Period filter (dropdown showing "Декабрь 2025")
- Data table with columns:
  - Код SKU (SKU code)
  - Наименование товара (Product name)
  - Начало товара 1 от основного месяца (Start of goods, first month)
  - Средние продажи за последние 3 мес (Average sales last 3 months)
  - Средние продажи за будущие 3 мес (Average sales future 3 months)
  - Рекомендуемое (Recommended amount)
  - Заказать в кол-ве (Order quantity)
- Numeric data in each cell

**Actions**:
- User adjusts filters → Table recalculates
- User edits "Order quantity" values → Updates pending order
- User clicks "Скачать Excel" → Export current view to Excel
- User submits orders (button not visible, but likely needed)

**States to handle**:
- **Loading**: While fetching order data
- **Empty**: No products need ordering for selected period
- **Error**: Failed to load supply chain data
- **Validation**: User enters invalid quantity

**Business rules affecting UI**:
- How is "recommended" amount calculated?
- Can users override recommended values freely?
- Are there min/max order constraints per product?
- Is there an order approval workflow?

---

### 8. Distribution by Branches (`/distribution`)
**Purpose**: Plan distribution of goods across branch locations

**Data I need to display**:
- Header summary:
  - Начало стока (Starting stock): Numeric value (50,8K)
  - Объём распределения (Distribution volume): Numeric value (20K)
- Table with columns:
  - № (Row number)
  - Филиал (Branch name)
  - Объем м3 (Volume in m3)
  - Сумма $ (Amount in dollars)
  - Рекомендуемый объем (Recommended volume)
  - Распределить в кол-ве (Distribute quantity)
  - Индекс здоровья (Health index): Status indicator with colored dot (Здоровый/Предупреждение/Критический)
- Multiple branch rows (Алматы Центр, Астана Север, Шымкент Юг, etc.)

**Actions**:
- User edits "Distribute quantity" → Updates distribution plan and health index
- User clicks edit icon next to branch → Modify branch details
- User clicks "Скачать Excel" → Export distribution plan

**States to handle**:
- **Loading**: While fetching distribution data
- **Empty**: No branches configured
- **Error**: Failed to load branches
- **Validation**: Total distributed exceeds available stock

**Business rules affecting UI**:
- How is "recommended volume" calculated per branch?
- What determines health index status?
- Can distribution exceed starting stock (backorder scenario)?
- Are there branch capacity constraints?

---

### 9. Inventory Health Index (`/inventory-health`)
**Purpose**: Monitor stock health across product categories and identify issues

**Data I need to display**:
- Top section with category cards (A, B, C):
  - Category label with percentage indicator (e.g., "Доля: 70%")
  - SKU count (e.g., "4 СКЮ (15% от итога)")
  - Total sales value
  - Товарный запас % (Inventory stock %)
  - Индекс Здоровья score
- Main table with columns:
  - Код (SKU code)
  - Наименование товара (Product name)
  - Категория (Category): Colored letter badge (A, B, C)
  - Продажи (Sales): Numeric value
  - Доля (Share): Percentage
  - Индекс Здоровья (Health Index): Numeric score
- Side panels showing:
  - "Излишки: ТОП 5 СКЮ" (Surplus: Top 5 SKUs)
  - "Дефицит: ТОП 5 СКЮ" (Deficit: Top 5 SKUs)
  - "Сток аут: ТОП 5 СКЮ" (Stock out: Top 5 SKUs)
  - Each showing product names with percentages

**Actions**:
- User scrolls through table → View all products
- User clicks on product → View detailed product analysis (uncertain if this exists)
- User filters by category (not shown but likely needed)

**States to handle**:
- **Loading**: While calculating inventory health
- **Empty**: No inventory data available
- **Error**: Failed to load health metrics

**Business rules affecting UI**:
- How are products categorized into A/B/C (ABC analysis logic)?
- What's the threshold for "surplus" vs "deficit" vs "stock out"?
- Is the health index a formula or manually set?
- Can users adjust category assignments?

---

### 10. Orders and Receipts (`/orders`)
**Purpose**: Track customer orders and incoming receipts with status management

**Data I need to display**:
Three tab views:

**Tab 1: "Заказы" (Orders)**
- Table with columns:
  - Номер заказа (Order number): Format "# 101", "# 102", etc.
  - Дата создания (Creation date)
  - Дата прихода (Arrival date)
  - Кол-во коробок (Box quantity)
  - Сумма $ (Amount)
  - Статус (Status): Colored indicator (Завершен/В транзите/Создан)

**Tab 2: "В транзите" (In Transit)**
- Same columns as Orders tab, filtered to show only in-transit orders

**Tab 3: "Приходы" (Receipts)**
- Same structure, showing completed receipts

- Date range filter at top (e.g., "Янв 2025 - Дек 2025")

**Actions**:
- User switches tabs → Filter orders by status
- User clicks order number → View order details (likely)
- User clicks "Загрузить Excel" → Import orders/receipts
- User adjusts date range → Table filters to that period

**States to handle**:
- **Loading**: While fetching order data
- **Empty**: No orders for selected period
- **Error**: Failed to load orders

**Business rules affecting UI**:
- What statuses are possible beyond shown ones?
- Can users manually change order status?
- Are there notification triggers (e.g., order delayed)?
- How are "receipts" different from "orders" in the data model?

---

### 11. Notifications (`/notifications`)
**Purpose**: Show system alerts, updates, and action items to user

**Data I need to display**:
- List of notification items, each showing:
  - Notification title/summary
  - Timestamp or date
  - Read/unread indicator (likely)
  - Type/category icon or color (if applicable)
  - Action buttons if notification requires response

**Actions**:
- User clicks notification → Mark as read and/or navigate to related item
- User dismisses notification → Remove from list
- User marks all as read → Clear unread indicators

**States to handle**:
- **Loading**: While fetching notifications
- **Empty**: No notifications ("You're all caught up!")
- **Error**: Failed to load notifications

**Business rules affecting UI**:
- What triggers notifications (new report, low stock, order delayed)?
- Do notifications expire after a certain time?
- Are there notification preferences users can configure?

---

### 12. Application Shell (Sidebar Navigation)
**Purpose**: Persistent navigation across all authenticated pages

**Data I need to display**:
- User profile information:
  - User's full name (e.g., "Danat K.")
  - User's role (e.g., "Demand planner")
  - User initials or avatar image
- Navigation menu items:
  - Дашборд (Dashboard)
  - ДП отчет (DP Report)
  - Ассортимент (Assortment)
  - Заказ (Orders)
  - Перемещение (Distribution/Movement)
  - Индекс здоровья (Inventory Health)
  - Приходы (Receipts/Arrivals)
  - Уведомления (Notifications) - with unread count badge?
  - Выйти (Logout)
- Active page indicator (bright green highlight)

**Actions**:
- User clicks nav item → Navigate to that page
- User clicks logout → Log out and return to login screen
- User clicks profile area → Show profile menu/settings (uncertain if this exists)

**Business rules affecting UI**:
- Are menu items role-based (some users see fewer options)?
- Should there be keyboard shortcuts for navigation?
- Can users collapse/expand the sidebar?

---

## Uncertainties

- [ ] **User roles and permissions**: Not clear if there are different permission levels (admin, planner, analyst, viewer). Do different users see different menu items or have restricted actions?

- [ ] **Data refresh frequency**: How often should dashboards/reports auto-refresh? Is there real-time data or is it batch-updated overnight?

- [ ] **Multi-language support**: Screenshots show Russian language. Is English or other language support needed? Should this affect backend API design?

- [ ] **Excel upload validation**: What happens when uploaded Excel has errors or missing columns? Should backend return detailed validation errors or just reject the file?

- [ ] **Forecast calculation logic**: How are the corrected forecasts generated? Is there a ML model backend runs, or do users manually adjust? Do I just display results or is there interactive adjustment?

- [ ] **Historical data retention**: How far back should data be available? Does this affect query performance considerations?

- [ ] **Concurrent editing**: Can multiple users edit the same report or distribution plan simultaneously? Should I handle optimistic locking or show "user X is editing" warnings?

- [ ] **Branch/warehouse hierarchy**: The screenshots show branch names, but is there a multi-level hierarchy (regions → branches → warehouses)? Does filtering need to support hierarchy?

- [ ] **Product categories**: Are the A/B/C categories the only ones, or are there custom category schemes? Can users define their own category rules?

- [ ] **Date ranges and fiscal calendar**: Should date pickers respect a fiscal year vs calendar year? Are there preset ranges like "Last Quarter" or "YTD"?

- [ ] **Export formats**: Should Excel exports include formatting/styling, or just raw data? Are there PDF export requirements?

- [ ] **Notification delivery**: Beyond in-app notifications, are there email or mobile push notifications? Does backend need to track notification delivery status?

---

## Questions for Backend

- **Authentication & sessions**: What's the preferred auth approach? JWT tokens, sessions, OAuth? Should tokens auto-refresh or require re-login?

- **Real-time updates**: For dashboards showing live metrics, should I poll periodically, use WebSockets, or SSE (Server-Sent Events)?

- **Pagination strategy**: For large tables (thousands of products), should I request paginated data or expect full datasets? What's a reasonable page size?

- **Caching strategy**: Which data changes frequently vs rarely? Can I cache assortment/product data client-side, or does it change too often?

- **Filtering/sorting location**: Should complex filtering (multiple conditions, date ranges) be done backend-side, or should I fetch everything and filter client-side?

- **Batch operations**: For updates to hundreds of distribution quantities or order amounts, should I send individual requests or batch updates in one request?

- **Error handling conventions**: What's the preferred error response format? Should I expect HTTP status codes + error messages, or structured error objects with field-level validation?

- **Date/time format**: Should I send dates in ISO 8601 format? What timezone handling is expected (UTC, user timezone, warehouse timezone)?

- **Numeric precision**: For financial amounts and percentages, how many decimal places should I expect and display? Should rounding happen backend or frontend?

- **File uploads**: For Excel imports, should I upload directly to backend, or get a signed URL to upload to cloud storage first?

- **Report storage**: Are saved reports stored as: raw parameters (re-query on view), snapshot of data (frozen at creation), or both?

- **Formula transparency**: For calculated fields (health index, recommended quantities), should I receive the formula/logic so I can show it to users, or just the result?

- **Soft deletes**: When users "delete" reports or items, are they soft-deleted (archived) or permanently removed? Should I still show them with a "deleted" indicator?

- **Audit trail**: Do you track who created/modified data? Should I display "Created by X on Y, last modified by Z on W"?

---

## Discussion Log

### 2026-02-09: Initial Requirements Created
- Frontend team documented requirements based on UI screenshots
- Awaiting backend team review and feedback
- Key areas needing clarification: auth strategy, user permissions, forecast calculation ownership, real-time data requirements

---

## Notes

**Open to suggestions**: This is our best understanding of the data needs based on the UI designs. If the data model we're imagining doesn't align with how things are actually structured on the backend, please push back and suggest a better approach.

**Collaboration over specification**: We'd rather have a conversation about what makes sense than rigidly stick to these requirements. Let us know if combining data differently, restructuring relationships, or simplifying requests would make more sense from a backend perspective.

**MVP scope**: If implementing all screens at once is too ambitious, we're open to phasing. Priority screens: Login → Dashboard → Assortment → Orders. Reports and health index can come in a second phase if that helps.
