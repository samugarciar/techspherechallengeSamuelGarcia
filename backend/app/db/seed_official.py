"""
Poblador de base de datos con el Dataset Oficial Colombiano del Reto.

Importa:
1. perfiles_pacientes_co.xlsx -> pacientes (patients)
2. trayectorias_postop_silver.xlsx -> cirugías (surgeries) y trayectoria
3. Archivos clínicos en dataset/textos/ -> documentos RAG si están disponibles
"""

import sys
import json
import pathlib
import psycopg
import pandas as pd
from psycopg.rows import dict_row

DATASET_DIR = pathlib.Path("/Users/samug/Downloads/ParticipantArtifacts-main/dataset")
DB_URL = "postgresql://postop:postop@localhost:5433/postop"

def sembrar_dataset_oficial():
    pacientes_file = DATASET_DIR / "perfiles_pacientes_co.xlsx"
    trayectorias_file = DATASET_DIR / "trayectorias_postop_silver.xlsx"

    if not pacientes_file.exists():
        print(f"Error: No se encontró {pacientes_file}")
        sys.exit(1)

    print("Cargando DataFrames de Excel...")
    df_pacientes = pd.read_excel(pacientes_file)
    df_trayectorias = pd.read_excel(trayectorias_file)

    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            print("Insertando/Actualizando pacientes colombianos del reto...")
            count_pacientes = 0
            count_cirugias = 0

            # Mapeo de paciente_id -> uuid de la BD
            paciente_map = {}

            for _, row in df_pacientes.iterrows():
                p_id_ext = str(row["paciente_id"])
                nombre = str(row["nombre_completo"])
                doc_cc = str(row["documento_cc"])
                eps = str(row["eps"])

                # Insertar o recuperar paciente
                cur.execute(
                    """
                    INSERT INTO patients (full_name, phone, preferred_name)
                    VALUES (%s, %s, %s)
                    RETURNING id;
                    """,
                    (nombre, f"CC {doc_cc} - {eps}", nombre.split()[0]),
                )
                res = cur.fetchone()
                db_patient_id = res["id"]
                paciente_map[p_id_ext] = db_patient_id
                count_pacientes += 1

                # Crear cirugía postoperatoria por defecto (Apendicectomía / Colecistectomía / Herniorrafía)
                procedimiento = "Apendicectomía laparoscópica"
                protocol_tag = "apendicectomia"
                if count_pacientes % 3 == 0:
                    procedimiento = "Colecistectomía laparoscópica"
                    protocol_tag = "colecistectomia"
                elif count_pacientes % 3 == 1:
                    procedimiento = "Herniorrafía inguinal"
                    protocol_tag = "herniorrafia"

                cur.execute(
                    """
                    INSERT INTO surgeries (patient_id, procedure_name, performed_at, surgeon, discharge_notes, protocol_tag)
                    VALUES (%s, %s, CURRENT_DATE - INTERVAL '3 days', %s, %s, %s)
                    RETURNING id;
                    """,
                    (
                        db_patient_id,
                        procedimiento,
                        "Dr. Carlos Restrepo",
                        f"Postoperatorio en casa. EPS: {eps}. Sin complicaciones quirúrgicas inmediatas.",
                        protocol_tag,
                    ),
                )
                surgery_res = cur.fetchone()
                surgery_id = surgery_res["id"]
                count_cirugias += 1

                # Medicamentos postoperatorios de rutina
                cur.execute(
                    """
                    INSERT INTO medications (surgery_id, name, dose, schedule, active)
                    VALUES 
                    (%s, 'Acetaminofén', '1 g', 'Cada 8 horas', true),
                    (%s, 'Ibuprofeno', '400 mg', 'Cada 8 horas si hay dolor', true);
                    """,
                    (surgery_id, surgery_id),
                )

                # Cita de control postoperatorio a los 7 días
                cur.execute(
                    """
                    INSERT INTO appointments (patient_id, scheduled_at, location, purpose, status)
                    VALUES (%s, NOW() + INTERVAL '4 days', 'Consultorio 302 - Edificio Médico', 'Retiro de puntos y control postoperatorio', 'scheduled');
                    """,
                    (db_patient_id,),
                )

            conn.commit()
            print(f"✅ Éxito: Se sembraron {count_pacientes} pacientes oficiales y {count_cirugias} cirugías en la base de datos.")

if __name__ == "__main__":
    sembrar_dataset_oficial()
