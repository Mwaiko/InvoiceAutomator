psql "postgresql://grn_db_user:ykNj3fBzPTAuG2aXiBNukuVQEZxuLkwP@dpg-d6vrl45m5p6s73ah11ng-a.virginia-postgres.render.com/grn_db?sslmode=require" -c "-- Fix invoices where payment_status = 'paid' but amounts are wrong
UPDATE etims_invoices
SET
    amount_paid      = invoice_amount,
    outstanding_amount = 0
WHERE
    payment_status = 'paid'
    AND (amount_paid != invoice_amount OR outstanding_amount != 0);

-- Sanity check: show any remaining inconsistencies after fix
SELECT id, invoice_number, invoice_amount, amount_paid, outstanding_amount, payment_status
FROM etims_invoices
WHERE
    (payment_status = 'paid' AND (amount_paid != invoice_amount OR outstanding_amount != 0))
 OR (payment_status != 'paid' AND outstanding_amount != (invoice_amount - amount_paid));"


psql "postgresql://grn_db_user:ykNj3fBzPTAuG2aXiBNukuVQEZxuLkwP@dpg-d6vrl45m5p6s73ah11ng-a.virginia-postgres.render.com/grn_db?sslmode=require" -c "UPDATE etims_invoices
SET amount_paid = invoice_amount
WHERE payment_status = 'paid'
  AND amount_paid != invoice_amount;

-- Verify — should return 0 rows after the fix
SELECT id, invoice_number, invoice_amount, amount_paid, payment_status
FROM etims_invoices
WHERE payment_status = 'paid'
  AND amount_paid != invoice_amount;"