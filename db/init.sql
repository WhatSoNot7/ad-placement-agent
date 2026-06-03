-- Инициализация БД с тестовыми данными

-- Таблица пользователей
CREATE TABLE IF NOT EXISTS users (
    user_id VARCHAR(50) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    branch VARCHAR(50) NOT NULL,
    role VARCHAR(20) NOT NULL CHECK (role IN ('editor', 'approver'))
);

-- Справочник домов
CREATE TABLE IF NOT EXISTS houses (
    house_id VARCHAR(20) PRIMARY KEY,
    branch VARCHAR(50) NOT NULL,
    address VARCHAR(200),
    apartments INTEGER NOT NULL,
    existing_subscribers INTEGER DEFAULT 0,
    has_technical_capability BOOLEAN DEFAULT TRUE
);

-- Планы (рабочие)
CREATE TABLE IF NOT EXISTS plans (
    id SERIAL PRIMARY KEY,
    branch VARCHAR(50) NOT NULL,
    month VARCHAR(7) NOT NULL,  -- YYYY-MM
    house_id VARCHAR(20) NOT NULL REFERENCES houses(house_id),
    ad_type VARCHAR(50) NOT NULL,
    frequency INTEGER DEFAULT 1,
    apartments INTEGER,
    existing_subscribers INTEGER,
    predicted_leads NUMERIC(10,2),
    cost NUMERIC(10,2),
    created_at TIMESTAMP DEFAULT NOW()
);

-- Финализированные планы
CREATE TABLE IF NOT EXISTS plans_final (
    id SERIAL PRIMARY KEY,
    branch VARCHAR(50) NOT NULL,
    month VARCHAR(7) NOT NULL,
    house_id VARCHAR(20) NOT NULL,
    ad_type VARCHAR(50) NOT NULL,
    frequency INTEGER DEFAULT 1,
    apartments INTEGER,
    existing_subscribers INTEGER,
    predicted_leads NUMERIC(10,2),
    cost NUMERIC(10,2),
    finalized_at TIMESTAMP DEFAULT NOW()
);

-- Лог корректировок
CREATE TABLE IF NOT EXISTS corrections_log (
    id SERIAL PRIMARY KEY,
    branch VARCHAR(50) NOT NULL,
    month VARCHAR(7) NOT NULL,
    editor_id VARCHAR(50) NOT NULL REFERENCES users(user_id),
    corrections_json JSONB NOT NULL,
    submitted_at TIMESTAMP DEFAULT NOW(),
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'modify')),
    reviewed_by VARCHAR(50),
    reviewed_at TIMESTAMP,
    UNIQUE(branch, month, editor_id)
);

-- Дедлайны
CREATE TABLE IF NOT EXISTS deadlines (
    id SERIAL PRIMARY KEY,
    branch VARCHAR(50) NOT NULL,
    month VARCHAR(7) NOT NULL,
    deadline_date DATE NOT NULL,
    UNIQUE(branch, month)
);

-- ============ ТЕСТОВЫЕ ДАННЫЕ ============

-- Пользователи
INSERT INTO users (user_id, name, branch, role) VALUES
    ('editor_nsk_01', 'Иванов Пётр', 'Новосибирск', 'editor'),
    ('editor_kzn_01', 'Сидорова Мария', 'Казань', 'editor'),
    ('editor_msk_01', 'Козлов Дмитрий', 'Москва', 'editor'),
    ('approver_01', 'Петрова Анна', 'HQ', 'approver')
ON CONFLICT DO NOTHING;

-- Дома
INSERT INTO houses (house_id, branch, address, apartments, existing_subscribers, has_technical_capability) VALUES
    ('NSK-001', 'Новосибирск', 'ул. Ленина, 10', 120, 45, TRUE),
    ('NSK-002', 'Новосибирск', 'ул. Красный проспект, 25', 80, 20, TRUE),
    ('NSK-003', 'Новосибирск', 'ул. Гоголя, 5', 200, 90, TRUE),
    ('NSK-004', 'Новосибирск', 'ул. Советская, 15', 60, 10, TRUE),
    ('NSK-005', 'Новосибирск', 'ул. Державина, 7', 150, 80, FALSE),
    ('KZN-001', 'Казань', 'ул. Баумана, 1', 100, 30, TRUE),
    ('KZN-002', 'Казань', 'ул. Пушкина, 12', 90, 25, TRUE),
    ('KZN-003', 'Казань', 'ул. Кремлёвская, 8', 110, 50, TRUE),
    ('MSK-001', 'Москва', 'ул. Тверская, 20', 300, 150, TRUE),
    ('MSK-002', 'Москва', 'ул. Арбат, 5', 180, 60, TRUE)
ON CONFLICT DO NOTHING;

-- План на июль 2025
INSERT INTO plans (branch, month, house_id, ad_type, frequency, apartments, existing_subscribers, predicted_leads, cost) VALUES
    ('Новосибирск', '2025-07', 'NSK-001', 'mailbox_flyer', 2, 120, 45, 3.2, 1500.00),
    ('Новосибирск', '2025-07', 'NSK-002', 'elevator_poster', 1, 80, 20, 1.8, 2000.00),
    ('Новосибирск', '2025-07', 'NSK-003', 'door_hanger', 1, 200, 90, 4.1, 3000.00),
    ('Новосибирск', '2025-07', 'NSK-004', 'mailbox_flyer', 1, 60, 10, 1.1, 800.00),
    ('Казань', '2025-07', 'KZN-001', 'elevator_poster', 1, 100, 30, 2.1, 1800.00),
    ('Казань', '2025-07', 'KZN-002', 'mailbox_flyer', 2, 90, 25, 2.4, 1200.00),
    ('Казань', '2025-07', 'KZN-003', 'door_hanger', 1, 110, 50, 2.0, 2500.00),
    ('Москва', '2025-07', 'MSK-001', 'elevator_poster', 2, 300, 150, 5.5, 5000.00),
    ('Москва', '2025-07', 'MSK-002', 'stairwell_banner', 1, 180, 60, 3.2, 3500.00)
ON CONFLICT DO NOTHING;

-- Дедлайны
INSERT INTO deadlines (branch, month, deadline_date) VALUES
    ('Новосибирск', '2025-07', '2025-06-25'),
    ('Казань', '2025-07', '2025-06-25'),
    ('Москва', '2025-07', '2025-06-25'),
    ('Новосибирск', '2025-08', '2025-07-25'),
    ('Казань', '2025-08', '2025-07-25'),
    ('Москва', '2025-08', '2025-07-25')
ON CONFLICT DO NOTHING;