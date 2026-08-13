-- Datos sintéticos para la demo. Tres pacientes con cirugías, medicación y
-- citas distintas — suficiente para que el agente tenga que llamar a las tools
-- y no pueda "adivinar" desde el contexto.
--
-- Cargar con:  docker exec -i postop_db psql -U postop -d postop < backend/app/db/seed.sql

BEGIN;

TRUNCATE patients, surgeries, medications, appointments, calls, call_turns RESTART IDENTITY CASCADE;

INSERT INTO patients (id, full_name, preferred_name, documento_cc, phone) VALUES
  ('aaaaaaaa-0000-0000-0000-000000000001', 'María Elena Restrepo Gómez', 'María', '1012345678', '+57 300 111 2233'),
  ('aaaaaaaa-0000-0000-0000-000000000002', 'Jorge Andrés Villalba Ruiz',  'Jorge', '1023456789', '+57 301 444 5566'),
  ('aaaaaaaa-0000-0000-0000-000000000003', 'Lucía Fernanda Ospina Marín', 'Lucía', '1034567890', '+57 302 777 8899');

INSERT INTO surgeries (id, patient_id, procedure_name, performed_at, surgeon, protocol_tag, discharge_notes) VALUES
  ('bbbbbbbb-0000-0000-0000-000000000001', 'aaaaaaaa-0000-0000-0000-000000000001',
   'Apendicectomía laparoscópica', CURRENT_DATE - 3, 'Dra. Carmona', 'apendicectomia',
   'Evolución intraoperatoria sin complicaciones. Tres puertos laparoscópicos.'),
  ('bbbbbbbb-0000-0000-0000-000000000002', 'aaaaaaaa-0000-0000-0000-000000000002',
   'Colecistectomía laparoscópica', CURRENT_DATE - 7, 'Dr. Peláez', 'colecistectomia',
   'Colelitiasis. Sin conversión a cirugía abierta.'),
  ('bbbbbbbb-0000-0000-0000-000000000003', 'aaaaaaaa-0000-0000-0000-000000000003',
   'Herniorrafia inguinal derecha', CURRENT_DATE - 1, 'Dra. Carmona', 'herniorrafia',
   'Malla de polipropileno. Alta el mismo día.');

INSERT INTO medications (surgery_id, name, dose, schedule, starts_on, ends_on, active) VALUES
  ('bbbbbbbb-0000-0000-0000-000000000001', 'Cefalexina',  '500 mg', 'cada 8 horas',  CURRENT_DATE - 3, CURRENT_DATE + 4, true),
  ('bbbbbbbb-0000-0000-0000-000000000001', 'Acetaminofén','1 g',    'cada 8 horas',  CURRENT_DATE - 3, CURRENT_DATE + 4, true),
  ('bbbbbbbb-0000-0000-0000-000000000002', 'Ibuprofeno',  '400 mg', 'cada 12 horas', CURRENT_DATE - 7, CURRENT_DATE - 2, false),
  ('bbbbbbbb-0000-0000-0000-000000000002', 'Omeprazol',   '20 mg',  'una vez al día',CURRENT_DATE - 7, CURRENT_DATE + 7, true),
  ('bbbbbbbb-0000-0000-0000-000000000003', 'Acetaminofén','500 mg', 'cada 6 horas',  CURRENT_DATE - 1, CURRENT_DATE + 5, true);

INSERT INTO appointments (patient_id, scheduled_at, location, purpose) VALUES
  ('aaaaaaaa-0000-0000-0000-000000000001', (CURRENT_DATE + 7)::timestamptz + interval '10 hours',
   'Consultorio 302, Torre B', 'Revisión de herida y retiro de puntos'),
  ('aaaaaaaa-0000-0000-0000-000000000002', (CURRENT_DATE + 3)::timestamptz + interval '15 hours 30 minutes',
   'Consultorio 210, Torre A', 'Control postoperatorio'),
  ('aaaaaaaa-0000-0000-0000-000000000003', (CURRENT_DATE + 9)::timestamptz + interval '9 hours',
   'Consultorio 302, Torre B', 'Revisión de malla');

COMMIT;

\echo 'Semilla cargada:'
SELECT p.preferred_name, s.procedure_name,
       (CURRENT_DATE - s.performed_at) || ' días postop' AS evolucion,
       (SELECT count(*) FROM medications m WHERE m.surgery_id = s.id AND m.active) AS meds_activas
FROM patients p JOIN surgeries s ON s.patient_id = p.id
ORDER BY s.performed_at DESC;
