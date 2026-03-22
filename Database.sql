-- ============================================================
--  Complete Database Schema
-- ============================================================

-- 1. Businesses
CREATE TABLE businesses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    kra_pin VARCHAR(50),
    email VARCHAR(255),
    phone VARCHAR(50),
    credit_limit DECIMAL(15,2) DEFAULT 0,
    payment_terms_days INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Branches
CREATE TABLE branches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    branch_name VARCHAR(255) NOT NULL,
    store_number VARCHAR(50),
    contact_person VARCHAR(255),
    phone VARCHAR(50),
    email VARCHAR(255),
    address TEXT,
    county VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 3. Products
CREATE TABLE products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    internal_code VARCHAR(100),
    etims_item_code VARCHAR(100),
    description VARCHAR(255) NOT NULL,
    uom VARCHAR(50) NOT NULL,
    default_unit_price DECIMAL(15,2) DEFAULT 0,
    tax_type VARCHAR(20) CHECK (tax_type IN ('VAT', 'NONVAT', 'ZERO')) DEFAULT 'NONVAT',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 4. Orders
CREATE TABLE orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id UUID NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    order_number VARCHAR(100) UNIQUE NOT NULL,
    lpo_number VARCHAR(100),
    order_date TIMESTAMP NOT NULL,
    status VARCHAR(20) CHECK (status IN ('draft', 'confirmed', 'delivered', 'closed')) DEFAULT 'draft',
    total_amount DECIMAL(15,2) DEFAULT 0,
    created_by UUID,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE order_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES products(id),
    quantity_ordered DECIMAL(15,2) NOT NULL,
    unit_price DECIMAL(15,2) NOT NULL,
    subtotal DECIMAL(15,2) NOT NULL
);

-- 5. GRNs (Customer Generated)
CREATE TABLE grns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    grn_number VARCHAR(100) UNIQUE NOT NULL,
    lpo_number VARCHAR(100),
    delivery_note_number VARCHAR(100),
    vendor_id VARCHAR(100),
    receipt_date DATE NOT NULL,
    store_name VARCHAR(255),
    store_location VARCHAR(255),
    subtotal DECIMAL(15,2) NOT NULL,
    vat_amount DECIMAL(15,2) DEFAULT 0,
    total_amount DECIMAL(15,2) NOT NULL,
    confirmed_by VARCHAR(255),
    is_locked BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE grn_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    grn_id UUID NOT NULL REFERENCES grns(id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES products(id),
    quantity_received DECIMAL(15,2) NOT NULL,
    unit_price DECIMAL(15,2) NOT NULL,
    net_amount DECIMAL(15,2) NOT NULL
);

-- 6. eTIMS Invoices (Generated from GRN)
CREATE TABLE etims_invoices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    grn_id UUID NOT NULL REFERENCES grns(id) ON DELETE CASCADE,
    invoice_number VARCHAR(150),
    scu_id VARCHAR(100),
    cu_invoice_number VARCHAR(150),
    invoice_date TIMESTAMP NOT NULL,
    invoice_type VARCHAR(20) CHECK (invoice_type IN ('NONVAT', 'VAT', 'ZERO')) DEFAULT 'NONVAT',
    taxable_amount DECIMAL(15,2) NOT NULL,
    tax_amount DECIMAL(15,2) DEFAULT 0,
    total_amount DECIMAL(15,2) NOT NULL,
    receipt_signature VARCHAR(255),
    internal_data VARCHAR(255),
    etims_status VARCHAR(20) CHECK (etims_status IN ('pending', 'submitted', 'approved', 'rejected')) DEFAULT 'pending',
    storage_path TEXT,
    etims_payload JSONB,
    etims_response JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE etims_invoice_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    etims_invoice_id UUID NOT NULL REFERENCES etims_invoices(id) ON DELETE CASCADE,
    item_code VARCHAR(100),
    description VARCHAR(255),
    quantity DECIMAL(15,2) NOT NULL,
    unit_price DECIMAL(15,2) NOT NULL,
    tax_rate DECIMAL(5,2) DEFAULT 0,
    tax_amount DECIMAL(15,2) DEFAULT 0,
    total_incl_tax DECIMAL(15,2) NOT NULL
);

-- 7. Payments
CREATE TABLE payments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    etims_invoice_id UUID NOT NULL REFERENCES etims_invoices(id) ON DELETE CASCADE,
    amount_paid DECIMAL(15,2) NOT NULL,
    payment_date DATE NOT NULL,
    payment_method VARCHAR(20) CHECK (payment_method IN ('bank', 'mpesa', 'cash')),
    reference_number VARCHAR(150),
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 8. Users
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    role VARCHAR(20) CHECK (role IN ('admin', 'sales', 'accountant')) NOT NULL,
    password VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes
CREATE INDEX idx_orders_branch    ON orders(branch_id);
CREATE INDEX idx_grns_order       ON grns(order_id);
CREATE INDEX idx_etims_grn        ON etims_invoices(grn_id);
CREATE INDEX idx_payments_invoice ON payments(etims_invoice_id);