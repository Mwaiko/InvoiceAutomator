psql "postgresql://grn_db_user:ykNj3fBzPTAuG2aXiBNukuVQEZxuLkwP@dpg-d6vrl45m5p6s73ah11ng-a.virginia-postgres.render.com/grn_db?sslmode=require" -c "
SELECT kra_invoice_no FROM etims_invoices
LIMIT 10;"