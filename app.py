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

# --- DATABASE ENGINE & MULTI-USER RELATION SCHEMAS ---
conn = sqlite3.connect("habit_v6_multiuser.db", check_same_thread=False)
cursor = conn.cursor()

# 1. Master Profiles Table
cursor.execute("""
    CREATE TABLE IF NOT EXISTS profiles (
        name TEXT PRIMARY KEY
    )
""")

# 2. Main expense logger table (Now includes profile, timestamp, and optional incident note)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile TEXT,
        timestamp TEXT,
        item_type TEXT,
        brand TEXT,
        count INTEGER,
        cost REAL,
        note TEXT
    )
""")

# 3. Relational Goals Table (Scoped per profile)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS goals (
        profile TEXT PRIMARY KEY,
        name TEXT,
        cost REAL
    )
""")

# 4. Scoped Dynamic Price Book (Keyed by profile, type, and brand)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS price_book (
        profile TEXT,
        item_type TEXT,
        brand TEXT,
        unit_price REAL,
        PRIMARY KEY (profile, item_type, brand)
    )
""")

# 5. Settings Table (Scoped per profile)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        profile TEXT,
        key TEXT,
        value TEXT,
        PRIMARY KEY (profile, key)
    )
""")
conn.commit()

# --- STREAMING_CHUNK: Managing user session state and profile provisioning...

# Initialize active profile states
if "active_profile" not in st.session_state:
    st.session_state.active_profile = "Default User"

# Ensure at least one profile exists in the DB
cursor.execute("SELECT COUNT(*) FROM profiles")
if cursor.fetchone()[0] == 0:
    cursor.execute("INSERT INTO profiles VALUES ('Default User')")
    conn.commit()

def provision_new_profile(profile_name: str):
    """
    Sets up default price catalogs, baseline budgets, and goals 
    for newly created profiles to prevent blank dashboard state errors.
    """
    # 1. Insert Profile row
    cursor.execute("INSERT OR IGNORE INTO profiles VALUES (?)", (profile_name,))
    
    # 2. Insert Default Goals
    cursor.execute("INSERT OR IGNORE INTO goals VALUES (?, 'Weekend Getaway Resort', 12000.0)", (profile_name,))
    
    # 3. Insert Baseline Budget
    cursor.execute("INSERT OR IGNORE INTO settings VALUES (?, 'baseline_budget', '4500.0')", (profile_name,))
    
    # 4. Insert default catalog prices
    default_prices = [
        (profile_name, "Cigar", "Classic", 18.0),
        (profile_name, "Cigar", "Marlboro", 22.0),
        (profile_name, "Cigar", "Dunhill", 30.0),
        (profile_name, "Cigar", "Generic", 18.0),
        (profile_name, "Drink", "Kingfisher", 160.0),
        (profile_name, "Drink", "Heineken", 220.0),
        (profile_name, "Drink", "Corona", 250.0),
        (profile_name, "Drink", "Budweiser", 180.0),
        (profile_name, "Drink", "Generic", 170.0)
    ]
    cursor.executemany("INSERT OR IGNORE INTO price_book VALUES (?, ?, ?, ?)", default_prices)
    conn.commit()

# Auto-provision default user if database was just generated
provision_new_profile("Default User")

# --- INITIALIZE GEMINI API ---
client = genai.Client()

class SmartExtractionSchema(BaseModel):
    cigarettes: int
    cigarette_brand: str
    drinks: int
    drink_brand: str
    health_insight: str

# --- STREAMING_CHUNK: Declaring data helper functions...

def get_brand_price(profile: str, item_type: str, brand_name: str) -> float:
    """
    Looks up price dynamically from active profile's price catalog.
    Falls back to 'Generic' in the respective category if unrecognized.
    """
    cursor.execute("SELECT unit_price FROM price_book WHERE profile = ? AND item_type = ? AND brand = ?", (profile, item_type, brand_name))
    row = cursor.fetchone()
    if row:
        return float(row[0])
    
    fallback_brand = "Generic"
    cursor.execute("SELECT unit_price FROM price_book WHERE profile = ? AND item_type = ? AND brand = ?", (profile, item_type, fallback_brand))
    fallback_row = cursor.fetchone()
    return float(fallback_row[0]) if fallback_row else 0.0

def parse_log_locally(text: str) -> SmartExtractionSchema:
    """
    Offline keyword map parser to calculate logs during API quota limits.
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

    # Match active brand keywords locally
    cursor.execute("SELECT brand FROM price_book WHERE profile = ? AND item_type = 'Cigar'", (st.session_state.active_profile,))
    registered_cigars = [row[0].lower() for row in cursor.fetchall()]
    cursor.execute("SELECT brand FROM price_book WHERE profile = ? AND item_type = 'Drink'", (st.session_state.active_profile,))
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

# --- STREAMING_CHUNK: Formatting side panel navigation, managers, and profiles...
with st.sidebar:
    st.header("👥 User Profiles Hub")
    
    # Profile Selector
    cursor.execute("SELECT name FROM profiles")
    all_profiles = [row[0] for row in cursor.fetchall()]
    
    active_selection = st.selectbox("Select Active Tracker Profile", all_profiles, index=all_profiles.index(st.session_state.active_profile))
    if active_selection != st.session_state.active_profile:
        st.session_state.active_profile = active_selection
        st.rerun()
        
    # Profile Creator
    with st.expander("➕ Create New Profile"):
        new_prof_input = st.text_input("Profile Name", placeholder="e.g. John Doe").strip()
        if st.button("Add Profile", use_container_width=True):
            if new_prof_input:
                provision_new_profile(new_prof_input)
                st.session_state.active_profile = new_prof_input
                st.success(f"Profile '{new_prof_input}' created!")
                st.rerun()
            else:
                st.warning("Please type a name.")

    st.markdown("---")
    st.header("⚙️ Profile Configuration")
    
    # 1. Scoped Budget
    cursor.execute("SELECT value FROM settings WHERE profile = ? AND key = 'baseline_budget'", (st.session_state.active_profile,))
    current_baseline = float(cursor.fetchone()[0])
    
    new_baseline = st.number_input("Original Monthly Habit Cost (₹)", min_value=100.0, value=current_baseline, step=100.0,
                                   help="How much did this profile spend monthly before tracking?")
    if new_baseline != current_baseline:
        cursor.execute("UPDATE settings SET value = ? WHERE profile = ? AND key = 'baseline_budget'", (str(new_baseline), st.session_state.active_profile))
        conn.commit()
        st.success("Spending baseline updated!")
        
    # 2. Relational Goal Target
    cursor.execute("SELECT name, cost FROM goals WHERE profile = ?", (st.session_state.active_profile,))
    current_goal = cursor.fetchone()
    
    new_goal_name = st.text_input("Saving Reward Target", value=current_goal[0])
    new_goal_cost = st.number_input("Target Cost (₹)", min_value=100.0, value=current_goal[1])
    
    if st.button("Update Profile Goal"):
        cursor.execute("UPDATE goals SET name = ?, cost = ? WHERE profile = ?", (new_goal_name, new_goal_cost, st.session_state.active_profile))
        conn.commit()
        st.success("Reward milestone updated!")

    # 3. Dynamic Price Book Scoped to Active Profile
    st.markdown("---")
    st.subheader("🏷️ Profile Product Catalog")
    
    df_prices = pd.read_sql_query("SELECT item_type as 'Type', brand as 'Brand', unit_price as 'Price (₹)' FROM price_book WHERE profile = ?", conn, params=(st.session_state.active_profile,))
    st.dataframe(df_prices, hide_index=True)
    
    st.write("**Edit Catalog Prices**")
    add_type = st.selectbox("Product Category", ["Cigar", "Drink"])
    add_brand = st.text_input("Brand / Product", placeholder="e.g. Jameson, Marlboro Light")
    add_price = st.number_input("Unit Price (₹)", min_value=1.0, value=50.0, step=10.0)
    
    if st.button("Save to Catalog"):
        if add_brand:
            brand_cleaned = add_brand.strip().capitalize()
            cursor.execute("INSERT OR REPLACE INTO price_book (profile, item_type, brand, unit_price) VALUES (?, ?, ?, ?)",
                           (st.session_state.active_profile, add_type, brand_cleaned, add_price))
            conn.commit()
            st.success(f"Saved: {brand_cleaned} now costs ₹{add_price}")
            st.rerun()
        else:
            st.warning("Please type a brand name.")

# --- STREAMING_CHUNK: Building application head banners and layout structures...
st.title("🫁 Vitals, Wealth & Habit Ecosystem")
st.markdown(f"👥 Tracking active for: **{st.session_state.active_profile}**")
st.markdown("---")

# Setup App Tab Views for clean layout splits
main_tab, report_tab = st.tabs(["🎙️ Quick Logger & Status", "📜 History & Report Center"])

# --- TAB 1: QUICK LOGGER & STATUS ---
with main_tab:
    left_panel, right_panel = st.columns([1, 1.2])

    with left_panel:
        st.subheader("🎙️ Voice & Text Incident Recorder")
        st.write("Record audio or type your habit log below (e.g. *'Had two Classic cigars and a Corona beer'*):")
        
        # Audio File input
        audio_file_buffer = st.audio_input("Record your logger update")
        text_input = st.text_input("Or type alternative entry:")
        
        # Friendly incident trigger helper
        incident_note = st.text_input("Optional friendly note / incident description (e.g. Birthday party, Work stress):")
        
        processed_text = ""
        data = None
        
        if audio_file_buffer:
            with st.spinner("Extracting parameters via Gemini..."):
                try:
                    uploaded_audio = client.files.upload(file=audio_file_buffer)
                    
                    response = client.models.generate_content(
                        model='gemini-2.0-flash',
                        contents=[uploaded_audio, "Analyze this audio log for consumption counts and brands."],
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=SmartExtractionSchema,
                            system_instruction=(
                                "Extract counts and brands. If user indicates they stayed clean, sober, or had zero usage, output 0. "
                                "Normalize brands to Capitalize case. Create a practical 1-sentence health feedback note."
                            )
                        )
                    )
                    data = SmartExtractionSchema.model_validate_json(response.text)
                    processed_text = "Voice processed successfully!"
                except APIError as e:
                    if e.code == 429:
                        st.warning("⚠️ API Daily Quota Exceeded. Audio logging requires API. Please use the text manual box below.")
                    else:
                        st.error(f"Audio Error: {e}")
                except Exception as e:
                    st.error(f"Audio Exception: {e}")
                    
        elif text_input and st.button("Submit Typed Log", use_container_width=True):
            with st.spinner("Analyzing text entry..."):
                try:
                    response = client.models.generate_content(
                        model='gemini-2.0-flash',
                        contents=text_input,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=SmartExtractionSchema,
                            system_instruction="Extract counts and brands. Normalize brands to Capitalize case. Create a practical 1-sentence health feedback note."
                        )
                    )
                    data = SmartExtractionSchema.model_validate_json(response.text)
                    processed_text = "Text log processed successfully!"
                except APIError as e:
                    if e.code == 429:
                        data = parse_log_locally(text_input)
                        processed_text = "Logged successfully using Offline Fallback Engine (Free-tier Quota Met)!"
                    else:
                        st.error(f"API Error: {e}")
                except Exception as e:
                    st.error(f"Text Exception: {e}")

        # Handle DB entry insertion
        if data:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            note_str = incident_note.strip() if incident_note else "Unspecified Trigger"
            
            if data.cigarettes > 0:
                cost = get_brand_price(st.session_state.active_profile, "Cigar", data.cigarette_brand) * data.cigarettes
                cursor.execute("INSERT INTO expenses (profile, timestamp, item_type, brand, count, cost, note) VALUES (?, ?, 'Cigar', ?, ?, ?, ?)",
                               (st.session_state.active_profile, now_str, data.cigarette_brand, data.cigarettes, cost, note_str))
                
            if data.drinks > 0:
                cost = get_brand_price(st.session_state.active_profile, "Drink", data.drink_brand) * data.drinks
                cursor.execute("INSERT INTO expenses (profile, timestamp, item_type, brand, count, cost, note) VALUES (?, ?, 'Drink', ?, ?, ?, ?)",
                               (st.session_state.active_profile, now_str, data.drink_brand, data.drinks, cost, note_str))
            
            if data.cigarettes == 0 and data.drinks == 0:
                cursor.execute("INSERT INTO expenses (profile, timestamp, item_type, brand, count, cost, note) VALUES (?, ?, 'Clean', 'None', 0, 0.0, ?)",
                               (st.session_state.active_profile, now_str, note_str))
                
            conn.commit()
            st.toast(processed_text, icon="✅")
            st.info(f"💡 **Health Assist Alert:** {data.health_insight}")

        # --- STREAMING_CHUNK: Displaying physical status metrics...
        st.markdown("---")
        st.subheader("🫁 Physiological Recovery Tracker")
        
        df_history = pd.read_sql_query("SELECT timestamp, item_type FROM expenses WHERE profile = ? ORDER BY timestamp DESC", conn, params=(st.session_state.active_profile,))
        
        clean_streak = 0
        if not df_history.empty:
            today_str = datetime.now().strftime("%Y-%m-%d")
            yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            
            # Extract date partition from timestamp column to verify streaking status
            recent_toxins = df_history[df_history['timestamp'].str.startswith(today_str) | df_history['timestamp'].str.startswith(yesterday_str)]
            recent_toxins = recent_toxins[recent_toxins['item_type'].isin(['Cigar', 'Drink'])]
            clean_streak = 0 if not recent_toxins.empty else 2

        if clean_streak > 0:
            st.success(f"🔥 Terrific work! You are sustaining a **{clean_streak}-Day Vitality Streak**.")
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

    # --- RIGHT PANEL: EXPENSE VECTORS ---
    with right_panel:
        st.subheader("📊 Expense Vector Analysis")
        
        df_analytics = pd.read_sql_query("SELECT item_type, brand, count, cost FROM expenses WHERE profile = ? AND item_type != 'Clean'", conn, params=(st.session_state.active_profile,))
        
        if not df_analytics.empty:
            total_spent = df_analytics['cost'].sum()
            
            col_m1, col_m2 = st.columns(2)
            col_m1.metric("Total Investment Lost", f"₹{total_spent:,.2f}")
            
            # Fetch baseline configuration safely
            cursor.execute("SELECT value FROM settings WHERE profile = ? AND key = 'baseline_budget'", (st.session_state.active_profile,))
            budget_baseline = float(cursor.fetchone()[0])
            saved_diverted = max(0.0, budget_baseline - total_spent)
            
            progress_percentage = min((saved_diverted / new_goal_cost), 1.0)
            col_m2.metric(f"Diverted to: {new_goal_name}", f"₹{saved_diverted:,.2f}", f"{progress_percentage * 100:.1f}% Funded")
            
            st.write(f"**Progress towards {new_goal_name} (₹{new_goal_cost:,.2f}):**")
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
            col_m1, col_m2 = st.columns(2)
            col_m1.metric("Total Investment Lost", "₹0.00")
            
            cursor.execute("SELECT value FROM settings WHERE profile = ? AND key = 'baseline_budget'", (st.session_state.active_profile,))
            budget_baseline = float(cursor.fetchone()[0])
            col_m2.metric(f"Diverted to: {new_goal_name}", f"₹{budget_baseline:,.2f}", "100.0% Funded")
            
            st.write(f"**Progress towards {new_goal_name} (₹{new_goal_cost:,.2f}):**")
            st.progress(min((budget_baseline / new_goal_cost), 1.0))
            
            st.info("📊 Log your first transaction to draw custom spending breakdown visual charts.")

# --- STREAMING_CHUNK: Building history lists, datetime filtering tools, and reporting downloaders...
with report_tab:
    st.subheader("📜 Detailed Expense History & Reporting Workspace")
    st.write("Browse, filter, and export transaction lists for the active profile.")
    
    # Load all records for active profile
    df_raw = pd.read_sql_query("""
        SELECT timestamp as 'Timestamp', item_type as 'Type', brand as 'Brand/Product', count as 'Quantity', cost as 'Total Cost (₹)', note as 'Context Note'
        FROM expenses 
        WHERE profile = ?
        ORDER BY timestamp DESC
    """, conn, params=(st.session_state.active_profile,))
    
    if not df_raw.empty:
        # Date filtering widgets
        df_raw['Timestamp'] = pd.to_datetime(df_raw['Timestamp'])
        
        min_date = df_raw['Timestamp'].min().date()
        max_date = df_raw['Timestamp'].max().date()
        
        c_dates = st.columns(2)
        start_filter = c_dates[0].date_input("Start Date Filter", min_date, min_value=min_date, max_value=max_date)
        end_filter = c_dates[1].date_input("End Date Filter", max_date, min_value=min_date, max_value=max_date)
        
        # Apply Date Filter (Converting dates safely)
        mask = (df_raw['Timestamp'].dt.date >= start_filter) & (df_raw['Timestamp'].dt.date <= end_filter)
        df_filtered = df_raw.loc[mask].copy()
        
        # Convert timestamp to string format for neat table display
        df_filtered['Timestamp'] = df_filtered['Timestamp'].dt.strftime("%Y-%m-%d %H:%M:%S")
        
        # Display table
        st.dataframe(df_filtered, use_container_width=True, hide_index=True)
        
        # Download CSV Report
        csv_buffer = df_filtered.to_csv(index=False).encode('utf-8')
        st.download_button(
            label="📥 Download Detailed CSV Report",
            data=csv_buffer,
            file_name=f"Habit_Expense_Report_{st.session_state.active_profile}_{start_filter}_to_{end_filter}.csv",
            mime="text/csv",
            use_container_width=True
        )
    else:
        st.info("No recorded logs in database for this profile. Submit updates in the Logger tab to populate reports.")
