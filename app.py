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

# --- DATABASE ENGINE ---
conn = sqlite3.connect("habit_v3.db", check_same_thread=False)
cursor = conn.cursor()
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
cursor.execute("CREATE TABLE IF NOT EXISTS goals (name TEXT, cost REAL)")
conn.commit()

# Ensure default wish goal exists if none set
cursor.execute("SELECT COUNT(*) FROM goals")
if cursor.fetchone()[0] == 0:
    cursor.execute("INSERT INTO goals VALUES ('Weekend Getaway Resort', 12000.0)")
    conn.commit()

# --- INITIALIZE GEMINI ---
client = genai.Client()

class SmartExtractionSchema(BaseModel):
    cigarettes: int
    cigarette_brand: str
    drinks: int
    drink_brand: str
    health_insight: str

# --- CONSTANTS: BRAND PRICE BOOK ---
PRICE_BOOK = {
    "cigar": {"Classic": 18.0, "Marlboro": 22.0, "Dunhill": 30.0, "Generic": 18.0},
    "drink": {"Kingfisher": 160.0, "Heineken": 220.0, "Corona": 250.0, "Budweiser": 180.0, "Generic": 170.0}
}

# --- SMART LOCAL FALLBACK PARSER ---
def parse_log_locally(text: str) -> SmartExtractionSchema:
    """
    Fallback parser using regex to extract parameters locally 
    when the Gemini API free tier daily quota is completely exhausted.
    """
    text_lower = text.lower()
    
    # Check for clean entries
    if any(word in text_lower for word in ["clean", "sober", "zero", "none", "no ", "stayed clean"]):
        return SmartExtractionSchema(
            cigarettes=0, cigarette_brand="None",
            drinks=0, drink_brand="None",
            health_insight="Offline Mode: Clean day detected! Excellent work protecting your physical health."
        )
    
    # Extract numbers
    numbers = [int(s) for s in re.findall(r'\b\d+\b', text_lower)]
    count1 = numbers[0] if len(numbers) > 0 else 1
    count2 = numbers[1] if len(numbers) > 1 else 1

    cigar_count, drink_count = 0, 0
    cigar_brand, drink_brand = "Generic", "Generic"

    # Match Cigar Brands
    cigar_brands = ["marlboro", "classic", "dunhill"]
    matched_cigar_brands = [b for b in cigar_brands if b in text_lower]
    
    # Match Drink Brands
    drink_brands = ["kingfisher", "heineken", "corona", "budweiser"]
    matched_drink_brands = [b for b in drink_brands if b in text_lower]

    # Simple keyword routing
    if "marlboro" in text_lower or "classic" in text_lower or "dunhill" in text_lower or "cigar" in text_lower or "smoke" in text_lower or "cigarette" in text_lower:
        cigar_count = count1
        if matched_cigar_brands:
            cigar_brand = matched_cigar_brands[0].capitalize()
        if "beer" in text_lower or "drink" in text_lower or "heineken" in text_lower or "kingfisher" in text_lower:
            drink_count = count2 if len(numbers) > 1 else 1
            if matched_drink_brands:
                drink_brand = matched_drink_brands[0].capitalize()
    else:
        # Default fallback assume drink if mentioned
        drink_count = count1
        if matched_drink_brands:
            drink_brand = matched_drink_brands[0].capitalize()

    return SmartExtractionSchema(
        cigarettes=cigar_count,
        cigarette_brand=cigar_brand,
        drinks=drink_count,
        drink_brand=drink_brand,
        health_insight="Offline Mode Parsing: Logged successfully. (API Daily Quota Exhausted)"
    )

# --- SIDEBAR CONFIGURATION ---
with st.sidebar:
    st.header("⚙️ App Settings & Goals")
    
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
    
    audio_file_buffer = st.audio_input("Record your entry")
    text_input = st.text_input("Or type alternative text entry here:")
    processed_text = ""
    data = None
    
    if audio_file_buffer:
        with st.spinner("Transcribing and extracting voice parameters via Gemini..."):
            try:
                uploaded_audio = client.files.upload(file=audio_file_buffer)
                
                response = client.models.generate_content(
                    model='gemini-2.0-flash',
                    contents=[uploaded_audio, "Analyze this voice log for cigarette or alcohol consumption counts and brand names."],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=SmartExtractionSchema,
                        system_instruction=(
                            "Extract details from audio logs. Map counts. If the user indicates they stayed clean, sober, or had zero usage, output 0 for counts. "
                            "Create an actionable, highly practical 1-sentence physical feedback note."
                        )
                    )
                )
                data = SmartExtractionSchema.model_validate_json(response.text)
                processed_text = "Voice processed successfully via AI!"
            except APIError as e:
                if e.code == 429:
                    st.warning("⚠️ API Quota completely exhausted for today. Audio files cannot be processed offline. Please use the text box below.")
                else:
                    st.error(f"Audio error: {e}")
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
                        system_instruction="Extract counts and brands. Create an actionable, highly practical 1-sentence physical feedback note."
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
        
        if data.cigarettes > 0:
            cost = PRICE_BOOK["cigar"].get(data.cigarette_brand, PRICE_BOOK["cigar"]["Generic"]) * data.cigarettes
            cursor.execute("INSERT INTO expenses (date, item_type, brand, count, cost) VALUES (?, 'Cigar', ?, ?, ?)",
                           (today, data.cigarette_brand, data.cigarettes, cost))
        if data.drinks > 0:
            cost = PRICE_BOOK["drink"].get(data.drink_brand, PRICE_BOOK["drink"]["Generic"]) * data.drinks
            cursor.execute("INSERT INTO expenses (date, item_type, brand, count, cost) VALUES (?, 'Drink', ?, ?, ?)",
                           (today, data.drink_brand, data.drinks, cost))
        
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
        
        theoretical_baseline = 4500.00 
        saved_diverted = max(0.0, theoretical_baseline - total_spent)
        
        goal_name, goal_cost = new_goal_name, new_goal_cost
        progress_percentage = min((saved_diverted / goal_cost), 1.0)
        
        col_m2.metric(f"Diverted to: {goal_name}", f"₹{saved_diverted:,.2f}", f"{progress_percentage * 100:.1f}% Funded")
        st.write(f"**Progress towards {goal_name}:**")
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
        st.info("📊 Your live charts and wishlist data will render here as soon as entries are recorded.")
