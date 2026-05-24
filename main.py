from qgis.core import *
import subprocess
import sqlite3
import os

# == Config == 
HABITATS_PATH   = ""
POINTS_PATH     = ""
SPATIALITE_DB   = ""   # will be created
OUTPUT_DB       = "" # Where presence/absence data gets saved
OUTPUT_SHP      = "" # Where voronoi polygons will be saved
SPECIES_FIELD   = "Latin"    # Latin name column in habitats
PLOT_NAME_FIELD = "id"     # plot name column in points
CHECKPOINT_EVERY = 100   # commit to disk every N plots

# == Clustering Config ==
RSCRIPT_PATH     = "/usr/bin/Rscript"
R_FILE_PATH      = ""
CLUSTER_CSV      = ""

# == Step 1: Import shapefiles into SpatiaLite via ogr2ogr ==
# Remove old db if rerunning
if os.path.exists(SPATIALITE_DB):
    os.remove(SPATIALITE_DB)

print("Importing habitats...")
subprocess.run([
    "/usr/bin/ogr2ogr", # check your distribution
    "-f", "SQLite",
    "-dsco", "SPATIALITE=YES",
    "-nln", "habitats",
    "-nlt", "MULTIPOLYGON",
    SPATIALITE_DB,
    HABITATS_PATH
], check=True)

print("Importing points...")
subprocess.run([
    "/usr/bin/ogr2ogr",
    "-f", "SQLite",
    "-update",          # append to db rather than overwrite
    "-nln", "points",
    SPATIALITE_DB,
    POINTS_PATH
], check=True)

print("Import done!")

# == Step 2: Points & Habitats intersection ==
print("Running spatial intersection...")
conn = sqlite3.connect(SPATIALITE_DB)
conn.enable_load_extension(True)

# Load spatialite extension - path varies by OS:
# Linux:   mod_spatialite.so
# Mac:     mod_spatialite.dylib
# Windows: mod_spatialite.dll
try:
    conn.load_extension("mod_spatialite.so")
except Exception as e:
    print(f"Could not load SpatiaLite extension: {e}")
    print("Try adjusting the extension path for your OS")
    raise

cur = conn.cursor()

# Manually create the spatial index virtual tables
cur.executescript("""
    CREATE VIRTUAL TABLE IF NOT EXISTS idx_habitats_geometry
        USING rtree(pkid, xmin, xmax, ymin, ymax);
        
    CREATE VIRTUAL TABLE IF NOT EXISTS idx_points_geometry
        USING rtree(pkid, xmin, xmax, ymin, ymax);
""")

# Populate habitats index
cur.execute("""
    INSERT OR IGNORE INTO idx_habitats_geometry
    SELECT ROWID,
        MbrMinX(geometry), MbrMaxX(geometry),
        MbrMinY(geometry), MbrMaxY(geometry)
    FROM habitats
    WHERE geometry IS NOT NULL
""")

# Populate points index  
cur.execute("""
    INSERT OR IGNORE INTO idx_points_geometry
    SELECT ROWID,
        MbrMinX(geometry), MbrMaxX(geometry),
        MbrMinY(geometry), MbrMaxY(geometry)
    FROM points
    WHERE geometry IS NOT NULL
""")

conn.commit()

# Verify
cur.execute("""
    SELECT name FROM sqlite_master 
    WHERE type='table' AND name LIKE 'idx_%'
""")
print("Index tables:", cur.fetchall())

# Get all unique Latin names for column headers
print("Fetching Latin names...")
cur.execute(f'SELECT DISTINCT "{SPECIES_FIELD}" FROM habitats WHERE "{SPECIES_FIELD}" IS NOT NULL ORDER BY "{SPECIES_FIELD}"')
latin_names = [row[0].strip() for row in cur.fetchall()]
print(f"Found {len(latin_names)} species")

# Get all plot names
cur.execute(f'SELECT "{PLOT_NAME_FIELD}" FROM points ORDER BY "{PLOT_NAME_FIELD}"')
plot_names = [row[0] for row in cur.fetchall()]
print(f"Found {len(plot_names)} plots")

# == Step 3: Write output to db ==
out_conn = sqlite3.connect(OUTPUT_DB)
out_cur = out_conn.cursor()

# Create presence/absence table
# One column per species, named by latin name
col_defs = ", ".join([f'"{sp}" INTEGER DEFAULT 0' for sp in latin_names])
cols = ", ".join([f'"{sp}"' for sp in latin_names])
placeholders = ", ".join(["?" for sp in latin_names])
out_cur.execute("DROP TABLE IF EXISTS presence_absence")
out_cur.execute(f"""
    CREATE TABLE presence_absence (
        plot_name TEXT PRIMARY KEY,
        {col_defs}
    )
""")
out_conn.commit()
print("Output table created!")

total = len(plot_names)
for i, plot_name in enumerate(plot_names):

    # Spatial intersection query for this plot
    cur.execute(f"""
    SELECT DISTINCT h."{SPECIES_FIELD}"
    FROM habitats h, points p
    JOIN idx_habitats_geometry idx 
        ON idx.pkid = h.ROWID
        AND idx.xmin <= MbrMaxX(p.geometry)
        AND idx.xmax >= MbrMinX(p.geometry)
        AND idx.ymin <= MbrMaxY(p.geometry)
        AND idx.ymax >= MbrMinY(p.geometry)
    WHERE p."{PLOT_NAME_FIELD}" = ?
      AND ST_Intersects(h.geometry, p.geometry)
      AND h."{SPECIES_FIELD}" IS NOT NULL
    """, (plot_name,))

    present = set(row[0].strip() for row in cur.fetchall())
    row = [plot_name] + [1 if sp in present else 0 for sp in latin_names]
    out_cur.execute(f"""
        INSERT OR REPLACE INTO presence_absence (plot_name, {cols})
        VALUES (?, {placeholders})
    """, row)

    # commit every so often
    if (i + 1) % CHECKPOINT_EVERY == 0:
        print(f"  {i+1}/{total} plots processed...")

out_conn.commit()
out_conn.close()
print(f"Output: {total} plots x {len(latin_names)} species")

# ================================
# ======= Clustering in R ========
import csv

# == Step 1: Run the R script ==
print("Running R clustering script...")
result = subprocess.run(
    [RSCRIPT_PATH, R_FILE_PATH],
    capture_output=True,
    text=True
)

print("STDOUT:", result.stdout)
print("STDERR:", result.stderr)

if result.returncode != 0:
    raise RuntimeError(f"R script failed with return code {result.returncode}")

print("R script completed successfully!")

# == Step 2: Verify csv was created ==
if not os.path.exists(CLUSTER_CSV):
    raise FileNotFoundError(f"Expected csv file not found at {CLUSTER_CSV}")

# == Step 3: Load csv into db ==
print("Loading cluster assignments into db...")

# Read CSV
with open(CLUSTER_CSV, "r") as f:
    reader = csv.DictReader(f)
    rows = list(reader)

print(f"Read {len(rows)} rows from csv")

# Create clusters table (drop if rerunning)
cur.execute("DROP TABLE IF EXISTS cluster_assignments")
cur.execute("""
    CREATE TABLE cluster_assignments (
        plot_name TEXT,
        cluster   INTEGER
    )
""")

# Insert rows
cur.executemany(
    "INSERT INTO cluster_assignments (plot_name, cluster) VALUES (?, ?)",
    [(r["plot_id"], int(r["cluster"])) for r in rows]
)
conn.commit()
print(f"Inserted {len(rows)} cluster assignments")

# == Step 4: Join clusters onto points layer ==
# Add cluster column to points if it doesn't exist yet
existing_cols = [row[1] for row in cur.execute("PRAGMA table_info(points)")]
if "cluster" not in existing_cols:
    cur.execute("ALTER TABLE points ADD COLUMN cluster INTEGER")
    print("Added cluster column to points table")

cur.execute(f"""
    UPDATE points
    SET cluster = (
        SELECT ca.cluster
        FROM cluster_assignments ca
        WHERE ca.plot_name = points."{PLOT_NAME_FIELD}"
    )
""")
conn.commit()

# Verify join worked
cur.execute("SELECT COUNT(*) FROM points WHERE cluster IS NULL")
nulls = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM points")
total = cur.fetchone()[0]
print(f"Join complete: {total - nulls}/{total} points matched, {nulls} unmatched")

# Peek at result
print("\nSample joined rows:")
for row in cur.execute(f'SELECT "{PLOT_NAME_FIELD}", cluster FROM points LIMIT 5'):
    print(" ", row)

print("\nCluster assignments joined into SpatiaLite.")

# == Step 5: Generate Voronoi polygons in SpatiaLite ==
print("\nGenerating Voronoi polygons...")
# Step 1: Collect all points into a single multipoint
print("Collecting points into multipoint...")
cur.execute("DROP TABLE IF EXISTS all_points_collected")
cur.execute("""
    CREATE TABLE all_points_collected AS
    SELECT ST_Collect(geometry) AS geometry
    FROM points
""")
conn.commit()

# Step 2: Run VoronojDiagram and get the result as WKB in Python
print("Generating Voronoi diagram...")
cur.execute("""
    SELECT ST_AsText(VoronojDiagram(geometry))
    FROM all_points_collected
""")
voronoi_wkt = cur.fetchone()[0]

# Step 3: Explode multipolygon into individual polygons in Python
print("Exploding Voronoi polygons...")
from shapely import wkt as shapely_wkt
from shapely.geometry import mapping

voronoi_multi = shapely_wkt.loads(voronoi_wkt)
polygons = list(voronoi_multi.geoms)
print(f"Got {len(polygons)} Voronoi polygons")

# Step 4: Create voronoi_raw table and insert one row per polygon
cur.execute("DROP TABLE IF EXISTS voronoi_raw")
cur.execute("""
    CREATE TABLE voronoi_raw (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        plot_name TEXT,
        cluster   INTEGER,
        geometry  BLOB
    )
""")

srid = cur.execute("""
    SELECT srid FROM geometry_columns 
    WHERE f_table_name = 'points'
""").fetchone()[0]

for poly in polygons:
    cur.execute("""
        INSERT INTO voronoi_raw (geometry)
        VALUES (ST_GeomFromText(?, ?))
    """, (poly.wkt, srid))
conn.commit()
print("Voronoi polygons inserted!")

# Step 3: Add plot name and cluster by spatial join back to points
print("Joining cluster labels back onto Voronoi polygons...")
cur.execute(f"""
    UPDATE voronoi_raw
    SET cluster = (
        SELECT p.cluster
        FROM points p
        WHERE ST_Intersects(voronoi_raw.geometry, p.geometry)
        LIMIT 1
    )
""")
conn.commit()

# Register geometry column
cur.execute("""
    SELECT RecoverGeometryColumn('voronoi_raw', 'geometry',
        (SELECT srid FROM geometry_columns WHERE f_table_name='points'),
        'POLYGON')
""")
conn.commit()
print("Voronoi polygons created!")

# == Step 6: Dissolve by cluster ==
print("\nDissolving by cluster...")
cur.execute("DROP TABLE IF EXISTS voronoi_dissolved")
cur.execute("""
    CREATE TABLE voronoi_dissolved AS
    SELECT
        cluster,
        ST_Union(geometry) AS geometry
    FROM voronoi_raw
    GROUP BY cluster
""")
conn.commit()

cur.execute("""
    SELECT RecoverGeometryColumn('voronoi_dissolved', 'geometry',
        (SELECT srid FROM geometry_columns WHERE f_table_name='points'),
        'MULTIPOLYGON')
""")
conn.commit()
conn.close()
print("Dissolve done!")

# == Step 7: Export dissolved layer to shapefile ==
print("\nExporting to shapefile...")

result = subprocess.run([
    "/usr/bin/ogr2ogr",
    "-f", "ESRI Shapefile",
    "-overwrite",
    OUTPUT_SHP,
    SPATIALITE_DB,
    "voronoi_dissolved"
], capture_output=True, text=True)

print("STDOUT:", result.stdout)
print("STDERR:", result.stderr)

if result.returncode != 0:
    raise RuntimeError(f"ogr2ogr export failed: {result.returncode}")

print(f"\nAll done! Voronoi shapefile saved to {OUTPUT_SHP}")
