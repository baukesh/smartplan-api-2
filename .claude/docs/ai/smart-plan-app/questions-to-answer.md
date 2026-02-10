# Smart Plan Backend Requirements - Questions & Uncertainties to Address

Please provide answers/clarifications for the following items. Once completed, we'll update the main backend requirements document.

---

## 🤔 Uncertainties

### General System

**1. User roles and permissions**
- Are there different permission levels (admin, planner, analyst, viewer)?
- Do different users see different menu items or have restricted actions?
- **Answer:**
- We are expecting different permission levels (admin, planner, analyst, viewer)
- No, all users (except for admin) will see same menu items but restricted actions. Planner and analyst should be allowed to all actions while viewers are not allowed to make changes. Admin should have an admin panel (will be implemented in future) where they can change the access and user roles/groups/companies/reports 

**2. Data refresh frequency**
- How often should dashboards/reports auto-refresh?
- Is there real-time data or is it batch-updated overnight?
- **Answer:**
- Dashboards/reports should auto-refresh when fresh data is uploaded. Also, there has to be a scheduled update of sales baseline forecasts each week (preferably Sunday 4AM).
- Near real time is expected whenever new data is uploaded. Otherwise it is batch-updated overnight.

**3. Multi-language support**
- Screenshots show Russian language. Is English or other language support needed?
- Should this affect backend API design?
- **Answer:**
- No, let's stick to Russian for now. We will add English later.
- No, backend API design should follow the best practices (I suppose using english for attribute names)

**4. Historical data retention**
- How far back should data be available?
- Does this affect query performance considerations?
- **Answer:**
- 3 years back
- I think 3 years monthly sales data should not really affect the performance but make sure to provide near real-time performance after uploading new data

**5. Concurrent editing**
- Can multiple users edit the same report or distribution plan simultaneously?
- Should I handle optimistic locking or show "user X is editing" warnings?
- **Answer:**
- Yes, multiple users should be able to edit simultaneously.
- Go with optimistic locking.

**6. Branch/warehouse hierarchy**
- Is there a multi-level hierarchy (regions → branches → warehouses)?
- Does filtering need to support hierarchy?
- **Answer:**
- There is a multi-level hierarchy but let's keep it simple for MVP phase and stick to only one level
- Yes, it should support

**7. Date ranges and fiscal calendar**
- Should date pickers respect a fiscal year vs calendar year?
- Are there preset ranges like "Last Quarter" or "YTD"?
- **Answer:**
- No, for MVP let's use only calendar year.
- No, not for MVP phase.

**8. Export formats**
- Should Excel exports include formatting/styling, or just raw data?
- Are there PDF export requirements?
- **Answer:**
- Excel exports should inlude just raw data for MVP phase
- No PDF export for MVP phase.

**9. Notification delivery**
- Beyond in-app notifications, are there email or mobile push notifications?
- Does backend need to track notification delivery status?
- **Answer:**
- No email or mobile push notifications for MVP phase.
- No need for backend to track notification delivery status.

---

### Login Screen

**10. Password reset**
- Should there be password reset functionality?
- **Answer:**
- Password reset functionality should be available but not for MVP phase

**11. Account lockout**
- Are there account lockout rules after failed attempts?
- **Answer:**
- Allow 50 failed attempts for account lockout rules

---

### Dashboard

**12. Health thresholds**
- What determines the "health" threshold percentages (e.g., 70% = good, 78% = warning)?
- **Answer:**
- Let's set threshold percentages as follows: 80% and above is good, between 60% and 80% is acceptable, below 60% is warning.

**13. Category labels**
- Are the category labels (A, B, C) configurable or fixed?
- **Answer:**
- They should be auto generated (but configurable later) by the Pareto principle: if we sort (descending) all SKUs according their total sales, category A products are the ones generating 70% of total sales, category B products - ones generating next 20% of total sales, category C - products generating remaining 10% of total sales. 

**14. KPI customization**
- Can users customize which KPIs appear on their dashboard?
- **Answer:**
- For MVP phase, users should not be able to customize KPIs on their dashboards

**15. Date restrictions**
- Should date ranges be restricted (e.g., only last 2 years of data)?
- **Answer:**
- Restrict date ranges to 2 years of future and 5 years of historical data

---

### Reports

**16. Edit/delete permissions**
- Can users edit/delete existing reports, or are they immutable once created?
- **Answer:**
- Users can edit/delete existing reports

**17. Ownership**
- Are reports owned by individual users or shared across team?
- **Answer:**
- Reports are shared across team

**18. Filtering/sorting**
- Should there be filtering/sorting by date, name, type for the reports list?
- **Answer:**
- No filtering for MVP phase

**19. Validation**
- What validation is needed before saving a report?
- **Answer:**
- No validation for MVP

**20. Draft vs finalized**
- Can reports be saved as drafts vs. finalized?
- **Answer:**
- Yes, reports can be saved as drafts

**21. Templates**
- Are there templates or presets users can start from?
- **Answer:**
- No templates or presets for MVP phase.

---

### Assortment & Products

**22. Excel upload validation**
- What happens when uploaded Excel has errors or missing columns?
- Should backend return detailed validation errors or just reject the file?
- **Answer:**
- Backend should return detailed validation errors showing exact details like missing columns, incorrect data types, etc.

**23. Product categories**
- Are the A/B/C categories the only ones, or are there custom category schemes?
- Can users define their own category rules?
- **Answer:**
- A/B/C categories are the only ones for MVP phase.
- A/B/C thresholds will be customizable but not in MVP phase

---

### Forecasting

**24. Forecast calculation logic**
- How are the corrected forecasts generated?
- Is there a ML model backend runs, or do users manually adjust?
- Do I just display results or is there interactive adjustment?
- **Answer:**
- Baseline forcasts should be generated by GPT-5.2 using OPENAI_API_KEY for future 12 months. Later users can adjust the forecasts and they will be saved as adjusted forecasts. Adjustment is interactive.
---

## ❓ Technical Questions for Backend

### Authentication

**1. Authentication & sessions**
- What's the preferred auth approach? JWT tokens, sessions, OAuth?
- Should tokens auto-refresh or require re-login?
- **Answer:**
- JWT tokens
- Auto-refresh if possible

---

### Data Fetching & Performance

**2. Real-time updates**
- For dashboards showing live metrics, should I poll periodically, use WebSockets, or SSE (Server-Sent Events)?
- **Answer:**
- Select the most optimized (fastest and reliable) approach.

**3. Pagination strategy**
- For large tables (thousands of products), should I request paginated data or expect full datasets?
- What's a reasonable page size?
- **Answer:**
- Allow full datasets for MVP phase. Pagination will be added later.

**4. Caching strategy**
- Which data changes frequently vs rarely?
- Can I cache assortment/product data client-side, or does it change too often?
- **Answer:**
- You can cache assortment/product data client-side

**5. Filtering/sorting location**
- Should complex filtering (multiple conditions, date ranges) be done backend-side, or should I fetch everything and filter client-side?
- **Answer:**
- Complex filtering should be done on backend-side.

---

### Data Updates

**6. Batch operations**
- For updates to hundreds of distribution quantities or order amounts, should I send individual requests or batch updates in one request?
- **Answer:**
- Allow both individual and batch updates

---

### API Conventions

**7. Error handling conventions**
- What's the preferred error response format?
- Should I expect HTTP status codes + error messages, or structured error objects with field-level validation?
- **Answer:**
- Structured error objects with field-level validation

**8. Date/time format**
- Should I send dates in ISO 8601 format?
- What timezone handling is expected (UTC, user timezone, warehouse timezone)?
- **Answer:**
- Use ISO 8601 format for dates.
- Use user timezone

**9. Numeric precision**
- For financial amounts and percentages, how many decimal places should I expect and display?
- Should rounding happen backend or frontend?
- **Answer:**
- Let's stick to 2 decimal places.
- Rounding should happend backend.

---

### File Handling

**10. File uploads**
- For Excel imports, should I upload directly to backend, or get a signed URL to upload to cloud storage first?
- **Answer:**
- Upload directly to backend

---

### Data Storage & History

**11. Report storage**
- Are saved reports stored as: raw parameters (re-query on view), snapshot of data (frozen at creation), or both?
- **Answer:**
- Raw parameters.

**12. Formula transparency**
- For calculated fields (health index, recommended quantities), should I receive the formula/logic so I can show it to users, or just the result?
- **Answer:**
- Show only the results

**13. Soft deletes**
- When users "delete" reports or items, are they soft-deleted (archived) or permanently removed?
- Should I still show them with a "deleted" indicator?
- **Answer:**
- Soft-deletion.
- No need for "deleted" indicatior for MVP phase.

**14. Audit trail**
- Do you track who created/modified data?
- Should I display "Created by X on Y, last modified by Z on W"?
- **Answer:**
- Track who created and modified data.
- Display created by X on Y, last modified by Z on W.

---

### Project Planning

**15. MVP phasing**
- Should we implement all screens at once, or phase the work?
- Suggested priority: Login → Dashboard → Assortment → Orders, then Reports and Health Index later.
- Do you agree with this phasing, or suggest a different approach?
- **Answer:**
- Select the the most optimized option

---

## Additional Notes/Comments

(Add any additional context, constraints, or information that might be relevant)

**Your notes:**
Following are examples of excel files that users upload:


Pasting some glossary notes additionally.
Technical Name	Glossary (General Name)	Description	Formula	Screen
fact_sales	Fact	Фактические продажи	Raw data	DP Report
forecast_sales	Forecast (baseline)	Базовый Прогноз по Будущим продажам (ГПТ)	"GPT API call. Key parameters: Fact (last 3 years), Available Stock. Result: baseline forecast for future 1 year
Assumption: GPT will recommend orders to maintain the baseline and assume that those orders will be placed timely"	DP Report
target_sales	Target	План продаж на месяц/квартал/год в деньгах/коробах	Raw data	DP Report
forecast_sales_adjusted	Forecast (adjusted)	Исправленный пользователем	Manual entry (override)	DP Report
current_available_stock	Available stock (past/present)	Кол-во товаров (коробки, CBM, $)	"Cs: amount of cases
$: DSP * q-ty"	DP Report
future_available_stock	Available stock (future)	Ожидаемое будущее кол-во товара	Текущий сток  + Заказы в транзите - Аджастед или Бэйзлайн форкаст	DP Report
cases_quantity	Cases	кол-во коробок	Суммирует кол-во коробок в заказе	Orders & Receivals
total_sum	Total Sum	Общее количество в деньгах	Суммирует кол-во позиций в заказе * ДСП (цена)	Orders & Receivals
order_status	Status	"Отбражает статус заказа: 4 Статуса
1. Ожидает Подтверждения
2. Создан (после подтверждения от ЛМ)
3. В транзите 
4. Прибыл"	Manual entry	Orders & Receivals
stock_future_minus_one	Наличие товара -1 от планируемого месяца	"Пример: Планируемый месяц заказа Декабрь 2025г.
Данный пункт покажет ожидаемое/фактическое наличие товара в Ноябре 2025г."	Текущий сток  + Заказы в транзите - Аджастед или Бэйзлайн форкаст	Supply chain
sales_avg_l3m	Средние продажи за последний 3 месяца	Пример: Средние продажи за последние 3 месяца от заданного периода	Average(L3M)	Supply chain
sales_avg_f3m	Средние продажи за будущие 3 месяца	Пример: Средние продажи за будущие 3 месяца от заданного периода	"Average(F3M)
Future 3 Months = Adjusted/Baseline Forecast* "	Supply chain
recommended_quantity	Рекомендуемое количество	Количество, которое наша платформа рекомендует заказать	"(adjusted/baseline forecast per SKU per branch - (Current + transit stock)) * stock norm coefficient
ignores negative values = no order"	Supply chain
final_quantity	Заказать количество	Финальный заказ в количество, который пользователь может сам поправить (базово показывает цифры от рекомендованного количества)	Manual entry	Supply chain
total_cbm	CBM	Показывает общее кол-во товара в куб. м в заказе для филиала	Суммирует кол-во куб. м. в заказе	Распределение по филиалам
total_recommended_quantity	Рекомендуемое количество	Количество, которое наша платформа рекомендует заказать в деньгах	Суммирует кол-во $ в заказе	Распределение по филиалам
recommended_quantity_adjusted	Распределить в кол-ве	При необходимости, поле для редактирования рекомендуемого количества в заказе	Manual entry	Распределение по филиалам
health_index_status	Индекс Здоровья	Статус на основе индекса здоровья на уровне филиалов	"1. abs_deviation per SKU = ABS((current_stock - [stock_norm * avg_adjusted_or_baseline_forecast_3m / 30]) / stock_norm)
2. avg_deviation per Group (A,B,C) = SUMPRODUCT(revenue_share_sku, abs_deviation_sku)
3. avg_deviation per Branch = avg_dev_A*0.6 + avg_dev_B*0.3 + avg_dev_C*0.1
4. Status = IF(avg_dev_branch <=0.10,""Healthy"",
 IF(avg_dev_branch <=0.30,""Normal"",
 ""Critical""))

Для forecast avg считаем будущие 3 месяцев"	Распределение по филиалам
current_available_stock_monetary_amount	Наличие Товара	Актуальное Кол-во товаров (в деньгах) на хабе	Суммирует кол-во позиций на хабе * ДСП (цена) 	Распределение по филиалам
total_sku_quantity	Объем Распределения	"Общее количество товаров, которые планируют распределить (в деньгах, коробах, СБМ, и грос вейт)
Цифра динамичная на основе изменений в кол-ве распределения по филиалам"	Суммирует кол-во распределения в деньгах, коробах, CBM, gross weight	Распределение по филиалам
current_available_stock_monetary_amount	Наличие Товара	Актуальное Кол-во товаров (в деньгах) на хабе	Суммирует кол-во позиций на складе * ДСП (цена) 	Inventory Health Index
total_overstock_amount	Overstock	Сумма всех Overstock товаров в деньгах	SUM(current_stock - [{stock_norm+30} * avg_adjusted_or_baseline_forecast_3m / 30] * ДСП (цена))	Inventory Health Index
total_understock_amount	Below stock norm	Сумма всех Below stock norm товаров в деньгах	SUM([stock_norm * avg_adjusted_or_baseline_forecast_3m / 30] * ДСП (цена) - current_stock)	Inventory Health Index
total_out_of_stock_amount	Out-of-stock	Сумма всех Out-of-stock товаров в деньгах 	SUM([stock_norm * avg_adjusted_or_baseline_forecast_3m / 30] * ДСП (цена) of each SKU)	Inventory Health Index
deviation_avg	Отклонения	Среднее отклонение учитывая долю выручки	avg_deviation = SUMPRODUCT(revenue_share_sku, abs_deviation_sku)	Inventory Health Index
health_index_status	Индекс Здоровья	Статус на основе индекса здоровья (отклонение) на уровне категории и SKU	"Status = IF(avg_dev_branch <=0.10,""Healthy"",
 IF(avg_dev_branch <=0.30,""Normal"",
 ""Critical""))"	Inventory Health Index
sales_plan	План Продаж	План продаж на месяц/квартал/год в деньгах/коробах	Raw data	Overview
sales_fact	Факт Продаж	Факт продаж на месяц/квартал/год в деньгах/коробах	Raw data	Overview
total_overstock	Overstock	Сумма всех Overstock товаров в деньгах	SUM(current_stock - [{stock_norm+30} * avg_adjusted_or_baseline_forecast_3m / 30] * ДСП (цена)) per each branch	Overview
total_understock	Understock	Сумма всех Understock товаров в деньгах	SUM([stock_norm * avg_adjusted_or_baseline_forecast_3m / 30] * ДСП (цена) - current_stock) per each branch	Overview
total_out_of_stock	Out-of-stock	Сумма всех Out-of-stock товаров в деньгах 	SUM([stock_norm * avg_adjusted_or_baseline_forecast_3m / 30] * ДСП (цена) of each SKU) per each branch	Overview
total_orders_monetary_amount	Orders $	Сумма всех заказов (в транзите + прибытие) в деньгах	SUM(orders)	Overview
total_orders_amount_in_transit	In transit $	Сумма всех заказов в транзите в деньгах	SUM(orders in transit)	Overview
total_orders_amount_receivals	Receivals $	Сумма всех заказов, которые прибыли в деньгах	SUM(orders received)	Overview
