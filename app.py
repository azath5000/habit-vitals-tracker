import streamlit as st
from google import genai
from google.genai import types
from google.genai.errors import APIError
from pydantic import BaseModel
import sqlite3
import pandas as pd
import plotly.express as px
from datetime import datetime, timedelta
import time

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

# --- ROBUST RETRY WRAPPER FOR API QUOTAS ---
def generate_content_with_retry(model, contents, config, max_retries=3, initial_delay=5):
    """
    Calls the Gemini API and handles 429 Resource Exhausted errors 
    by waiting and retrying automatically.
    """
    delay = initial_delay
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config
            )
            return response
        except APIError as e:
            # Check for 429 Rate Limit / Resource Exhausted
            if e.code == 429:
                if attempt < max_retries - 1:
                    st.warning(f"⚠️ Quota limit hit. Automatically retrying in {delay} seconds... (Attempt {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                    delay *= 2  # Exponential backoff
                    continue
            raise e
    raise Exception("Max retries exceeded for Gemini API due to rate limits. Please try again in a minute.")

# --- CONSTANTS: BRAND PRICE BOOK ---
PRICE_BOOK = {
    "cigar": {"Classic": 18.0, "Marlboro": 22.0, "Dunhill": 30.0, "Generic": 18.0},
    "drink": {"Kingfisher": 160.0, "Heineken": 220.0, "Corona": 250.0, "Budweiser": 180.0, "Generic": 170.0}
}

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
                
                config = types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=SmartExtractionSchema,
                    system_instruction=(
                        "Extract details from audio logs. Map counts. If the user indicates they stayed clean, sober, or had zero usage, output 0 for counts. "
                        "Create an actionable, highly practical 1-sentence physical feedback note."
                    )
                )
                
                response = generate_content_with_retry(
                    model='gemini-2.0-flash',
                    contents=[uploaded_audio, "Analyze this voice log for cigarette or alcohol consumption counts and brand names."],
                    config=config
                )
                data = SmartExtractionSchema.model_validate_json(response.text)
                processed_text = "Voice processed successfully!"
            except Exception as e:
                st.error(f"Audio processing mistake: {e}")
                
    elif text_input and st.button("Submit Typed Log", use_container_width=True):
        with st.spinner("Analyzing text entry..."):
            try:
                config = types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=SmartExtractionSchema,
                    system_instruction="Extract counts and brands. Create an actionable, highly practical 1-sentence physical feedback note."
                )
                
                response = generate_content_with_retry(
                    model='gemini-2.0-flash',
                    contents=text_input,
                    config=config
                )
                data = SmartExtractionSchema.model_validate_json(response.text)
                processed_text = "Text log processed successfully!"
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
