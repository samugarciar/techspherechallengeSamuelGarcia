"""
Poblador de base de datos con el Dataset Oficial Colombiano del Reto.

Une `perfiles_pacientes_co.xlsx` y `perfiles_clinicos_pacientes_silver_contest.xlsx`
para repoblar las tablas `patients`, `surgeries`, `medications` y `appointments`
con los datos exactos del concurso y números de documento de ciudadanía.
"""

import pathlib
import sys

import pandas as pd
import psycopg
from psycopg.rows import dict_row

DATASET_DIR = pathlib.Path("/Users/samug/Downloads/ParticipantArtifacts-main/dataset")
DB_URL = "postgresql://postop:postop@localhost:5433/postop"

# Mapeo de módulo / procedimiento a etiqueta de protocolo RAG
MAPA_PROTOCOLOS = {
    "appendicitis": "apendicectomia",
    "Apendicectomía": "apendicectomia",
    "cholecystitis": "colecistectomia",
    "Colecistectomía": "colecistectomia",
    "colorectal_cancer": "colectomia",
    "Colectomía": "colectomia",
    "total_joint_replacement": "reemplazo_articular",
    "Reemplazo de cadera/rodilla": "reemplazo_articular",
    "breast_cancer": "mastectomia",
    "Mastectomía": "mastectomia",
}


def sembrar_dataset_oficial():
    pacientes_file = DATASET_DIR / "perfiles_pacientes_co.xlsx"
    clinico_file = DATASET_DIR / "perfiles_clinicos_pacientes_silver_contest.xlsx"

    if not pacientes_file.exists() or not clinico_file.exists():
        print(f"Error: Archivos no encontrados en {DATASET_DIR}")
        sys.exit(1)

    print("Cargando DataFrames del dataset oficial...")
    df_pac = pd.read_excel(pacientes_file)
    df_cli = pd.read_excel(clinico_file)

    # Merge por paciente_id
    df_merged = pd.merge(df_pac, df_cli, on="paciente_id", how="inner")

    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            print("Limpiando tablas de pacientes previas...")
            cur.execute("TRUNCATE patients, surgeries, medications, appointments CASCADE;")

            print(f"Insertando {len(df_merged)} pacientes del dataset oficial...")
            count_pacientes = 0
            count_cirugias = 0

            for idx, row in df_merged.iterrows():
                nombre = str(row["nombre_completo"])
                doc_cc = str(row["documento_cc"])
                eps = str(row["eps"])
                ciudad = str(row["ciudad"])
                departamento = str(row["departamento"])
                edad = int(row["edad"])
                genero = str(row.get("genero", "M"))

                phone = f"+57 3{idx:02d} {doc_cc[:3]} {doc_cc[3:7]}"
                preferred_name = nombre.split()[0]

                cur.execute(
                    """
                    INSERT INTO patients (full_name, documento_cc, phone, preferred_name)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id;
                    """,
                    (nombre, doc_cc, phone, preferred_name),
                )
                res_p = cur.fetchone()
                patient_id = res_p["id"]
                count_pacientes += 1

                # Cirugía asociada
                procedimiento = str(row["procedimiento"])
                modulo = str(row.get("modulo_synthea", "appendicitis"))
                protocol_tag = MAPA_PROTOCOLOS.get(procedimiento, MAPA_PROTOCOLOS.get(modulo, "apendicectomia"))

                cirujano = f"Dr. Carlos Restrepo ({eps})"
                notas = (
                    f"Postoperatorio en casa. Ciudad: {ciudad} ({departamento}). "
                    f"Paciente de {edad} años ({genero}). EPS: {eps}. Comorbilidades: {row.get('comorbilidades', '[]')}."
                )

                cur.execute(
                    """
                    INSERT INTO surgeries (patient_id, procedure_name, performed_at, surgeon, discharge_notes, protocol_tag)
                    VALUES (%s, %s, CURRENT_DATE - INTERVAL '3 days', %s, %s, %s)
                    RETURNING id;
                    """,
                    (patient_id, procedimiento, cirujano, notas, protocol_tag),
                )
                res_s = cur.fetchone()
                surgery_id = res_s["id"]
                count_cirugias += 1

                # Medicamentos activos
                cur.execute(
                    """
                    INSERT INTO medications (surgery_id, name, dose, schedule, active)
                    VALUES 
                    (%s, 'Acetaminofén', '1 g', 'Cada 8 horas', true),
                    (%s, 'Ibuprofeno', '400 mg', 'Cada 8 horas si hay dolor', true);
                    """,
                    (surgery_id, surgery_id),
                )

                # Cita de control postoperatorio a los 4 días
                cur.execute(
                    """
                    INSERT INTO appointments (patient_id, scheduled_at, location, purpose, status)
                    VALUES (%s, NOW() + INTERVAL '4 days', 'Consultorio 302 - Edificio Médico', 'Retiro de puntos y control postoperatorio', 'scheduled');
                    """,
                    (patient_id,),
                )

            conn.commit()
            print(f"✅ Éxito: Base de datos repoblada con {count_pacientes} pacientes oficiales y {count_cirugias} cirugías vinculadas.")


if __name__ == "__main__":
    sembrar_dataset_oficial()
