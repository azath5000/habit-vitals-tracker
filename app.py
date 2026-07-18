import streamlit as st
from google import genai
from google.genai import types
from google.genai.errors import APIError
from pydantic import BaseModel
import sqlite3
import pandas as pd
import plotly.express as px
from datetime import datetime, timedelta
import re

# --- INITIAL SETUP & THEME ---
st.set_page_config(page_title="Vitals & Wealth Tracker", layout="wide", initial_sidebar_state="expanded")

# --- DATABASE ENGINE & MIGRATIONS ---
conn = sqlite3.connect("habit_v4_advanced.db", check_same_thread=False)
cursor = conn.cursor()

# 1. Main expense transaction logger table
cursor.execute("""
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        item_type TEXT,
        brand TEXT,
        count INTEGER,
        cost REAL
    )
""")

# 2. Financial Wishlist target goals table
cursor.execute("CREATE TABLE IF NOT EXISTS goals (name TEXT, cost REAL)")

# 3. Dynamic Price Book table to allow user configuration of brands & prices
cursor.execute("""
    CREATE TABLE IF NOT EXISTS price_book (
        item_type TEXT,
        brand TEXT PRIMARY KEY,
        unit_price REAL
    )
""")

# 4. Global Settings table (used for original baseline budgets)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
""")
conn.commit()

# --- POPULATE DEFAULT VALUES (ON FIRST RUN) ---
# Default dream milestones
cursor.execute("SELECT COUNT(*) FROM goals")
if cursor.fetchone()[0] == 0:
    cursor.execute("INSERT INTO goals VALUES ('Weekend Getaway Resort', 12000.0)")
    conn.commit()

# Default spending baseline (money user spent per month before tracking)
cursor.execute("SELECT COUNT(*) FROM settings WHERE key = 'baseline_budget'")
if cursor.fetchone()[0] == 0:
    cursor.execute("INSERT INTO settings VALUES ('baseline_budget', '4500.0')")
    conn.commit()

# Default Brand Catalog & Price structures
cursor.execute("SELECT COUNT(*) FROM price_book")
if cursor.fetchone()[0] == 0:
    default_prices = [
        # Cigars / Cigarettes (Base units)
        ("Cigar", "Classic", 18.0),
        ("Cigar", "Marlboro", 22.0),
        ("Cigar", "Dunhill", 30.0),
        ("Cigar", "Generic", 18.0),
        # Alcoholic beverages / Drinks (Base units)
        ("Drink", "Kingfisher", 160.0),
        ("Drink", "Heineken", 220.0),
        ("Drink", "Corona", 250.0),
        ("Drink", "Budweiser", 180.0),
        ("Drink", "Generic", 170.0)
    ]
    cursor.executemany("INSERT INTO price_book VALUES (?, ?, ?)", default_prices)
    conn.commit()

# --- INITIALIZE GEMINI API ---
# Ensure your GEMINI_API_KEY environment variable is set in the terminal
client = genai.Client()

class SmartExtractionSchema(BaseModel):
    cigarettes: int
    cigarette_brand: str
    drinks: int
    drink_brand: str
    health_insight: str

def get_brand_price(item_type: str, brand_name: str) -> float:
    """
    Looks up price dynamically from database.
    If brand is unrecognized, falls back to the 'Generic' price of that category.
    """
    cursor.execute("SELECT unit_price FROM price_book WHERE brand = ?", (brand_name,))
    row = cursor.fetchone()
    if row:
        return float(row[0])
    
    # Fallback to category defaults if brand is missing or custom typed
    fallback_brand = "Generic"
    cursor.execute("SELECT unit_price FROM price_book WHERE item_type = ? AND brand = ?", (item_type, fallback_brand))
    fallback_row = cursor.fetchone()
    return float(fallback_row[0]) if fallback_row else 0.0

# --- SMART LOCAL FALLBACK PARSER (QUOTA FAILURE INSURANCE) ---
def parse_log_locally(text: str) -> SmartExtractionSchema:
    """
    Fallback parser using local keyword maps to calculate cost when API quota is exhausted.
    """
    text_lower = text.lower()
    
    if any(word in text_lower for word in ["clean", "sober", "zero", "none", "no ", "stayed clean"]):
        return SmartExtractionSchema(
            cigarettes=0, cigarette_brand="None",
            drinks=0, drink_brand="None",
            health_insight="Offline Mode: Clean day processed! Fantastic work saving your health and money."
        )
    
    numbers = [int(s) for s in re.findall(r'\b\d+\b', text_lower)]
    count1 = numbers[0] if len(numbers) > 0 else 1
    count2 = numbers[1] if len(numbers) > 1 else 1

    cigar_count, drink_count = 0, 0
    cigar_brand, drink_brand = "Generic", "Generic"

    # Fetch registered brands dynamically from DB to search text
    cursor.execute("SELECT brand FROM price_book WHERE item_type = 'Cigar'")
    registered_cigars = [row[0].lower() for row in cursor.fetchall()]
    cursor.execute("SELECT brand FROM price_book WHERE item_type = 'Drink'")
    registered_drinks = [row[0].lower() for row in cursor.fetchall()]

    matched_cigars = [b for b in registered_cigars if b in text_lower]
    matched_drinks = [b for b in registered_drinks if b in text_lower]

    if any(k in text_lower for k in ["cigar", "smoke", "cigarette", "marlboro", "classic", "dunhill"]):
        cigar_count = count1
        if matched_cigars:
            cigar_brand = matched_cigars[0].capitalize()
        if any(k in text_lower for k in ["beer", "drink", "whiskey", "heineken", "kingfisher", "corona"]):
            drink_count = count2 if len(numbers) > 1 else 1
            if matched_drinks:
                drink_brand = matched_drinks[0].capitalize()
    else:
        drink_count = count1
        if matched_drinks:
            drink_brand = matched_drinks[0].capitalize()

    return SmartExtractionSchema(
        cigarettes=cigar_count, cigarette_brand=cigar_brand,
        drinks=drink_count, drink_brand=drink_brand,
        health_insight="Offline Fallback Mode: Custom parser resolved input. (Gemini Quota Limited)"
    )

# --- SIDEBAR CONFIGURATION & CONFIG EDITORS ---
with st.sidebar:
    st.header("⚙️ Configuration Hub")
    
    # 1. Spend Budget Settings
    st.subheader("💰 Baseline Budget")
    cursor.execute("SELECT value FROM settings WHERE key = 'baseline_budget'")
    current_baseline = float(cursor.fetchone()[0])
    
    new_baseline = st.number_input("Your Original Monthly Spend (₹)", min_value=100.0, value=current_baseline, step=100.0,
                                   help="How much money did you use to spend on cigarettes & drinks monthly before tracking?")
    if new_baseline != current_baseline:
        cursor.execute("UPDATE settings SET value = ? WHERE key = 'baseline_budget'", (str(new_baseline),))
        conn.commit()
        st.success("Spending baseline updated!")
        
    # 2. Dream Milestones Setting
    st.subheader("🎯 Dream Reward Goal")
    cursor.execute("SELECT name, cost FROM goals LIMIT 1")
    current_goal = cursor.fetchone()
    
    new_goal_name = st.text_input("What are you saving for?", value=current_goal[0])
    new_goal_cost = st.number_input("Goal Target Cost (₹)", min_value=100.0, value=current_goal[1])
    
    if st.button("Update Goal Target"):
        cursor.execute("DELETE FROM goals")
        cursor.execute("INSERT INTO goals VALUES (?, ?)", (new_goal_name, new_goal_cost))
        conn.commit()
        st.success("Reward milestone updated!")

    # 3. Dynamic Price Book Manager (Users can view, add, or delete product catalogs)
    st.markdown("---")
    st.subheader("🏷️ Custom Product Catalog")
    
    # View Existing Prices
    df_prices = pd.read_sql_query("SELECT item_type as 'Type', brand as 'Brand', unit_price as 'Price (₹)' FROM price_book", conn)
    st.dataframe(df_prices, hide_index=True)
    
    # Form to Add/Update Brand
    st.write("**Add or Edit Brand Prices**")
    add_type = st.selectbox("Product Type", ["Cigar", "Drink"])
    add_brand = st.text_input("Brand Name", placeholder="e.g. Whiskey, Winston")
    add_price = st.number_input("Price per Unit / glass (₹)", min_value=1.0, value=20.0, step=5.0)
    
    if st.button("Save Brand to Catalog"):
        if add_brand:
            brand_cleaned = add_brand.strip().capitalize()
            cursor.execute("INSERT OR REPLACE INTO price_book (item_type, brand, unit_price) VALUES (?, ?, ?)",
                           (add_type, brand_cleaned, add_price))
            conn.commit()
            st.success(f"Saved: {brand_cleaned} now costs ₹{add_price} each")
            st.rerun()
        else:
            st.warning("Please type a brand name.")

    # Option to Delete Brand
    st.write("**Delete a Brand**")
    delete_brand = st.selectbox("Choose brand to delete", df_prices["Brand"].tolist())
    if st.button("Delete Brand"):
        if delete_brand in ["Generic", "None"]:
            st.error("Cannot delete system default 'Generic' or 'None' fallbacks.")
        else:
            cursor.execute("DELETE FROM price_book WHERE brand = ?", (delete_brand,))
            conn.commit()
            st.success(f"Deleted {delete_brand} from catalog.")
            st.rerun()

# --- APP HEADER ---
st.title("🫁 Vitals, Wealth & Habit Ecosystem")
st.write("Voice log your day instantly. Track your spending, visualize physical recovery, and watch your savings fund your next reward.")
st.markdown("---")

# Main Page Split Layout
left_panel, right_panel = st.columns([1, 1.2])

# --- LEFT PANEL: ZERO-FRICTION CAPTURE ---
with left_panel:
    st.subheader("🎙️ Instant Native Voice Logger")
    st.write("Click the record button below to log naturally (e.g., *'Had two Marlboros and a Heineken'* or *'I stayed clean today!'*):")
    
    # Streamlit native browser microphone capture element
    audio_file_buffer = st.audio_input("Record your entry")
    text_input = st.text_input("Or type alternative text entry here:")
    processed_text = ""
    data = None
    
    if audio_file_buffer:
        with st.spinner("Transcribing and extracting voice parameters via Gemini..."):
            try:
                uploaded_audio = client.files.upload(file=audio_file_buffer)
                
                # Request Structured extraction
                response = client.models.generate_content(
                    model='gemini-2.0-flash',
                    contents=[uploaded_audio, "Analyze this voice log for cigarette or alcohol consumption counts and brand names."],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=SmartExtractionSchema,
                        system_instruction=(
                            "Extract details from audio logs. Map counts. If the user indicates they stayed clean, sober, or had zero usage, output 0 for counts. "
                            "Normalize brands to Capitalize case. Create an actionable, highly practical 1-sentence physical feedback note."
                        )
                    )
                )
                data = SmartExtractionSchema.model_validate_json(response.text)
                processed_text = "Voice processed successfully via AI!"
            except APIError as e:
                if e.code == 429:
                    st.warning("⚠️ API Quota completely exhausted for today. Audio files cannot be processed offline. Please use the text input box below to log manually.")
                else:
                    st.error(f"Audio Error: {e}")
            except Exception as e:
                st.error(f"Audio processing mistake: {e}")
                
    elif text_input and st.button("Submit Typed Log", use_container_width=True):
        with st.spinner("Analyzing text entry..."):
            try:
                response = client.models.generate_content(
                    model='gemini-2.0-flash',
                    contents=text_input,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=SmartExtractionSchema,
                        system_instruction="Extract counts and brands. Normalize brands to Capitalize case. Create an actionable, highly practical 1-sentence physical feedback note."
                    )
                )
                data = SmartExtractionSchema.model_validate_json(response.text)
                processed_text = "Text log processed successfully via AI!"
            except APIError as e:
                if e.code == 429:
                    # Switch seamlessly to local parsing engine
                    data = parse_log_locally(text_input)
                    processed_text = "Logged successfully using Offline Fallback Engine (Free-tier Quota Met)!"
                else:
                    st.error(f"API Error: {e}")
            except Exception as e:
                st.error(f"Text processing mistake: {e}")

    # Handle Database Insertion
    if data:
        today = datetime.now().strftime("%Y-%m-%d")
        
        # Calculate cigar expense dynamically based on user database configuration prices
        if data.cigarettes > 0:
            cig_unit_cost = get_brand_price("Cigar", data.cigarette_brand)
            cost = cig_unit_cost * data.cigarettes
            cursor.execute("INSERT INTO expenses (date, item_type, brand, count, cost) VALUES (?, 'Cigar', ?, ?, ?)",
                           (today, data.cigarette_brand, data.cigarettes, cost))
            
        # Calculate drink expense dynamically based on user database configuration prices
        if data.drinks > 0:
            drink_unit_cost = get_brand_price("Drink", data.drink_brand)
            cost = drink_unit_cost * data.drinks
            cursor.execute("INSERT INTO expenses (date, item_type, brand, count, cost) VALUES (?, 'Drink', ?, ?, ?)",
                           (today, data.drink_brand, data.drinks, cost))
        
        # Clean day recording logic to maintain Streak visual updates
        if data.cigarettes == 0 and data.drinks == 0:
            cursor.execute("INSERT INTO expenses (date, item_type, brand, count, cost) VALUES (?, 'Clean', 'None', 0, 0.0)", (today,))
            
        conn.commit()
        st.toast(processed_text, icon="✅")
        st.info(f"💡 **Health Assist Alert:** {data.health_insight}")

    # --- GAMIFIED HEALTH METERS ---
    st.markdown("---")
    st.subheader("🫁 Physiological Recovery Tracker")
    
    df_history = pd.read_sql_query("SELECT date, item_type FROM expenses ORDER BY date DESC", conn)
    
    clean_streak = 0
    if not df_history.empty:
        today_str = datetime.now().strftime("%Y-%m-%d")
        yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        recent_toxins = df_history[df_history['date'].isin([today_str, yesterday_str]) & (df_history['item_type'].isin(['Cigar', 'Drink']))]
        clean_streak = 0 if not recent_toxins.empty else 2

    if clean_streak > 0:
        st.success(f"🔥 Terrific work! You are currently sustaining a **{clean_streak}-Day Vitality Streak**.")
        oxygen_recovery = min(clean_streak * 35, 100)
        cardio_recovery = min(clean_streak * 20, 100)
    else:
        st.warning("⚠️ Toxins detected recently. Biological regeneration cycles are resetting.")
        oxygen_recovery = 12
        cardio_recovery = 8
        
    st.write("**Blood Oxygenation Efficiency Level:**")
    st.progress(oxygen_recovery / 100.0)
    st.write("**Cardiovascular Stress Decompression Status:**")
    st.progress(cardio_recovery / 100.0)

# --- RIGHT PANEL: METRICS & VISUALIZATIONS ---
with right_panel:
    st.subheader("📊 Expense Vector Analysis")
    
    df_analytics = pd.read_sql_query("SELECT item_type, brand, count, cost FROM expenses WHERE item_type != 'Clean'", conn)
    
    if not df_analytics.empty:
        total_spent = df_analytics['cost'].sum()
        
        col_m1, col_m2 = st.columns(2)
        col_m1.metric("Total Investment Lost", f"₹{total_spent:,.2f}")
        
        # Pull customized spending baseline budget dynamically from DB settings table
        cursor.execute("SELECT value FROM settings WHERE key = 'baseline_budget'")
        budget_baseline = float(cursor.fetchone()[0])
        
        # Calculate saved vs baseline savings
        saved_diverted = max(0.0, budget_baseline - total_spent)
        
        goal_name, goal_cost = new_goal_name, new_goal_cost
        progress_percentage = min((saved_diverted / goal_cost), 1.0)
        
        col_m2.metric(f"Diverted to: {goal_name}", f"₹{saved_diverted:,.2f}", f"{progress_percentage * 100:.1f}% Funded")
        st.write(f"**Progress towards {goal_name} (₹{goal_cost:,.2f}):**")
        st.progress(progress_percentage)
        
        df_analytics['Label'] = df_analytics['item_type'] + " [" + df_analytics['brand'] + "]"
        chart_summary = df_analytics.groupby('Label')['cost'].sum().reset_index()
        
        pie_fig = px.pie(
            chart_summary,
            values='cost',
            names='Label',
            hole=0.45,
            title="Capital Consumption Mapping",
            color_discrete_sequence=px.colors.sequential.Oranges_r
        )
        st.plotly_chart(pie_fig, use_container_width=True)
    else:
        # Default metric layout showing savings metrics even if zero logged transactions exist
        col_m1, col_m2 = st.columns(2)
        col_m1.metric("Total Investment Lost", "₹0.00")
        
        cursor.execute("SELECT value FROM settings WHERE key = 'baseline_budget'")
        budget_baseline = float(cursor.fetchone()[0])
        saved_diverted = budget_baseline  # 100% saved since nothing is logged yet!
        
        goal_name, goal_cost = new_goal_name, new_goal_cost
        progress_percentage = min((saved_diverted / goal_cost), 1.0)
        
        col_m2.metric(f"Diverted to: {goal_name}", f"₹{saved_diverted:,.2f}", f"{progress_percentage * 100:.1f}% Funded")
        st.write(f"**Progress towards {goal_name} (₹{goal_cost:,.2f}):**")
        st.progress(progress_percentage)
        
        st.info("📊 Log your first transaction to draw custom spending breakdown visual charts.")
