import base64

def set_background():
    with open("background.png", "rb") as f:
        data = base64.b64encode(f.read()).decode()

    st.markdown(f"""
    <style>
    .stApp {{
        background-image:
            linear-gradient(rgba(0,0,0,0.35), rgba(0,0,0,0.15)),
            url("data:image/png;base64,{data}");
        background-size: cover;
        background-position: center;
        background-attachment: fixed;
    }}
    </style>
    """, unsafe_allow_html=True)
    from __future__ import annotations

"""
Assistant Voyage V4.1 — produit perso + architecture API + secrets cloud
=======================================================================

Objectifs :
- mémoire locale du profil utilisateur
- suggestions dynamiques de destinations
- comparaison multi-destinations
- scoring global : destination + vols + hôtels + fatigue + logistique
- UX en onglets
- lecture robuste des secrets en local et sur Streamlit Cloud
- architecture prête pour Skyscanner (vols) et Expedia Rapid (hôtels)
- mode démo si API non configurée

Dépendances :
    pip install streamlit pandas requests python-dotenv

Lancement :
    streamlit run travel_assistant_v4.py
"""

from dataclasses import dataclass, asdict, field
from datetime import date, datetime, time
from pathlib import Path
from typing import List, Optional, Literal, Dict, Tuple, Any
import hashlib
import json
import os
import time as time_module

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
PROFILE_PATH = DATA_DIR / "user_profile.json"
REQUEST_TIMEOUT_SECONDS = 20

TravelerPace = Literal["slow", "balanced", "intense"]
CabinType = Literal["economy", "premium_economy", "business", "any"]
HotelStyle = Literal["budget", "comfort", "premium", "boutique", "any"]
TripStyle = Literal["culture", "nature", "hiking", "seaside", "food", "romantic", "mixed"]


@dataclass
class HomeContext:
    city: str
    country: str = "France"
    nearest_airports: List[str] = field(default_factory=list)
    typical_transfer_cost_eur: float = 0.0


@dataclass
class TravelPreferences:
    adults: int = 2
    children: int = 0
    budget_total_eur: Optional[float] = None
    max_total_travel_time_hours: float = 8.0
    preferred_departure_windows: List[str] = field(default_factory=lambda: ["morning", "midday"])
    preferred_return_windows: List[str] = field(default_factory=lambda: ["afternoon", "evening"])
    avoid_red_eye: bool = True
    avoid_tight_connections: bool = True
    min_connection_minutes: int = 75
    cabin: CabinType = "economy"
    hotel_style: HotelStyle = "comfort"
    pace: TravelerPace = "balanced"
    trip_style: TripStyle = "mixed"
    wants_city_center: bool = True
    wants_breakfast_included: bool = False
    wants_free_cancellation: bool = True
    values_direct_flights: bool = True
    weight_price: int = 30
    weight_schedule: int = 20
    weight_logistics: int = 15
    weight_hotel: int = 10
    weight_fatigue: int = 10
    weight_destination: int = 15


@dataclass
class UserProfile:
    traveler_name: str = "Adrien & Mélanie"
    home_city: str = "Vecoux"
    home_country: str = "France"
    nearest_airports: List[str] = field(default_factory=lambda: ["BSL", "SXB", "FRA", "LUX"])
    transfer_cost_eur: float = 40.0
    default_budget_eur: float = 900.0
    default_trip_style: TripStyle = "mixed"
    default_pace: TravelerPace = "balanced"
    default_hotel_style: HotelStyle = "comfort"


@dataclass
class TripRequest:
    origin_context: HomeContext
    destination_city: str
    destination_country: str
    departure_date: date
    return_date: date
    preferences: TravelPreferences
    notes: str = ""


@dataclass
class DestinationInsight:
    destination_city: str
    destination_country: str
    airport_code: str
    culture_score: float
    nature_score: float
    hiking_score: float
    seaside_score: float
    food_score: float
    weather_feel_score: float
    romance_score: float
    family_friendly_score: float
    top_highlights: List[str]
    best_nature_spots: List[str]
    best_hikes: List[str]
    best_visits: List[str]
    summary: str

    @property
    def overall_leisure_score(self) -> float:
        vals = [
            self.culture_score,
            self.nature_score,
            self.hiking_score,
            self.seaside_score,
            self.food_score,
            self.weather_feel_score,
            self.romance_score,
        ]
        return round(sum(vals) / len(vals), 1)


@dataclass
class FlightOption:
    source: str
    origin_airport: str
    destination_airport: str
    airline: str
    price_eur_total: float
    outbound_departure: datetime
    outbound_arrival: datetime
    inbound_departure: datetime
    inbound_arrival: datetime
    stops_outbound: int
    stops_inbound: int
    connection_minutes_outbound: Optional[int] = None
    connection_minutes_inbound: Optional[int] = None
    baggage_included: bool = True

    @property
    def total_air_duration_hours(self) -> float:
        outbound = (self.outbound_arrival - self.outbound_departure).total_seconds() / 3600
        inbound = (self.inbound_arrival - self.inbound_departure).total_seconds() / 3600
        return round(outbound + inbound, 2)


@dataclass
class HotelOption:
    source: str
    name: str
    nightly_price_eur: float
    total_price_eur: float
    district: str
    walking_minutes_to_center: int
    breakfast_included: bool
    review_score_10: float
    cancellation_flexible: bool
    style: str


@dataclass
class TravelPlan:
    destination: DestinationInsight
    flight: FlightOption
    hotel: HotelOption
    total_estimated_price_eur: float
    score: float
    subscores: Dict[str, float]
    rationale: List[str]
    downside: str
    category: str


def clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))


def to_dt(d: date, hour: int, minute: int) -> datetime:
    return datetime.combine(d, time(hour=hour, minute=minute))


def classify_time_window(dt: datetime) -> str:
    h = dt.hour
    if 5 <= h < 11:
        return "morning"
    if 11 <= h < 15:
        return "midday"
    if 15 <= h < 20:
        return "afternoon"
    if 20 <= h <= 23:
        return "evening"
    return "night"


def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    env_value = os.getenv(name)
    if env_value:
        return env_value
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return default


def load_profile() -> UserProfile:
    if PROFILE_PATH.exists():
        try:
            data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
            return UserProfile(**data)
        except Exception:
            pass
    return UserProfile()


def save_profile(profile: UserProfile) -> None:
    PROFILE_PATH.write_text(json.dumps(asdict(profile), indent=2, ensure_ascii=False), encoding="utf-8")


# =========================================================
# Catalogue destinations
# =========================================================

def destination_catalog() -> List[DestinationInsight]:
    return [
        DestinationInsight("Palerme", "Italie", "PMO", 9.0, 8.0, 7.5, 8.8, 9.2, 8.7, 8.6, 8.2,
                           ["Centre historique", "Marchés siciliens", "Mondello", "Monreale"],
                           ["Mondello", "Capo Gallo", "Monte Pellegrino"],
                           ["Monte Pellegrino", "Capo Gallo"],
                           ["Cathédrale", "Palais des Normands", "Chapelle Palatine", "Monreale"],
                           "Excellent équilibre culture, gastronomie, mer et escapades nature."),
        DestinationInsight("Lisbonne", "Portugal", "LIS", 9.0, 7.8, 6.7, 7.9, 8.8, 8.4, 8.7, 8.0,
                           ["Alfama", "Belém", "Miradouros", "Tram 28"],
                           ["Sintra", "Cascais", "Costa da Caparica"],
                           ["Sintra", "Cabo da Roca"],
                           ["Belém", "Alfama", "Chiado", "Hiéronymites"],
                           "Très belle ville pour court séjour romantique avec bonnes excursions autour."),
        DestinationInsight("Funchal", "Portugal", "FNC", 7.6, 9.4, 9.5, 7.6, 8.4, 8.8, 8.4, 7.6,
                           ["Levadas", "Vieille ville", "Jardins", "Points de vue"],
                           ["Pico do Arieiro", "Laurisilva", "São Lourenço"],
                           ["Arieiro → Ruivo", "25 Fontes", "São Lourenço"],
                           ["Téléphérique", "Monte Palace", "Marché"],
                           "Destination reine pour randonnée et paysages spectaculaires."),
        DestinationInsight("Naples", "Italie", "NAP", 8.8, 8.2, 7.8, 7.8, 9.4, 8.3, 8.2, 7.6,
                           ["Centre historique", "Pompéi", "Vésuve", "Capri"],
                           ["Vésuve", "Capri", "Côte amalfitaine"],
                           ["Vésuve", "Sentier des Dieux"],
                           ["Pompéi", "Musée archéologique", "Spaccanapoli"],
                           "Très dense culturellement, superbe base pour excursions iconiques."),
        DestinationInsight("Athènes", "Grèce", "ATH", 9.2, 7.0, 6.4, 7.6, 8.7, 8.6, 8.0, 7.7,
                           ["Acropole", "Plaka", "Musées", "Riviera"],
                           ["Cap Sounion", "Hydra"],
                           ["Lycabette", "Filopappou"],
                           ["Acropole", "Agora", "Parthénon", "Musée archéologique"],
                           "Excellente destination patrimoine + soleil pour ville historique courte."),
        DestinationInsight("Barcelone", "Espagne", "BCN", 8.9, 7.4, 6.2, 8.2, 8.6, 8.3, 8.4, 8.3,
                           ["Sagrada Família", "Quartier gothique", "Montjuïc", "Barceloneta"],
                           ["Montjuïc", "Collserola"],
                           ["Collserola", "Montjuïc"],
                           ["Sagrada Família", "Parc Güell", "Born", "Gothique"],
                           "Ville très complète et très efficace pour court séjour mixte."),
        DestinationInsight("Séville", "Espagne", "SVQ", 9.1, 6.9, 5.8, 6.1, 8.8, 8.2, 8.8, 7.5,
                           ["Alcázar", "Cathédrale", "Santa Cruz", "Flamenco"],
                           ["Guadalquivir", "Parcs urbains"],
                           ["Balades urbaines"],
                           ["Alcázar", "Cathédrale", "Plaza de España"],
                           "Superbe ville romantique et culturelle, très adaptée aux city-breaks."),
        DestinationInsight("Catane", "Italie", "CTA", 8.0, 8.9, 8.8, 8.2, 8.9, 8.5, 8.1, 7.8,
                           ["Etna", "Taormine", "Centre baroque", "Plages"],
                           ["Etna", "Alcantara", "Taormine"],
                           ["Randos Etna", "Sentiers Alcantara"],
                           ["Catane centre", "Taormine", "Siracuse en extension"],
                           "Très forte option pour mix volcan, nature, mer et gastronomie."),
    ]


class DestinationAdvisor:
    def suggest(self, departure_date: date, return_date: date, preferences: TravelPreferences, max_results: int = 6) -> List[Tuple[float, DestinationInsight]]:
        candidates = destination_catalog()
        scored: List[Tuple[float, DestinationInsight]] = []
        trip_len = (return_date - departure_date).days

        for d in candidates:
            score = d.overall_leisure_score * 10
            style_map = {
                "culture": d.culture_score,
                "nature": d.nature_score,
                "hiking": d.hiking_score,
                "seaside": d.seaside_score,
                "food": d.food_score,
                "romantic": d.romance_score,
                "mixed": d.overall_leisure_score,
            }
            score += (style_map[preferences.trip_style] - 7.0) * 5

            if preferences.pace == "slow":
                score += ((d.seaside_score + d.nature_score + d.romance_score) / 3 - 7.0) * 4
            elif preferences.pace == "balanced":
                score += ((d.culture_score + d.food_score + d.nature_score) / 3 - 7.0) * 4
            else:
                score += ((d.culture_score + d.hiking_score) / 2 - 7.0) * 4

            if trip_len <= 3 and d.culture_score >= 8.8:
                score += 2
            if trip_len <= 3 and d.hiking_score > 9:
                score -= 3

            scored.append((round(score, 1), d))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:max_results]


# =========================================================
# API clients (live-ready structure)
# =========================================================

class SkyscannerClient:
    def __init__(self, api_key: Optional[str]):
        self.api_key = api_key

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def search_flights_live(self, req: TripRequest, destination_airport: str) -> List[FlightOption]:
        if not self.is_configured():
            return []
        # Intégration live à brancher quand les accès partenaires seront activés.
        # Gardé volontairement en stub pour éviter des appels faux ou incomplets.
        return []


class ExpediaRapidClient:
    def __init__(self, api_key: Optional[str], shared_secret: Optional[str]):
        self.api_key = api_key
        self.shared_secret = shared_secret

    def is_configured(self) -> bool:
        return bool(self.api_key and self.shared_secret)

    def auth_header(self) -> Dict[str, str]:
        timestamp = str(int(time_module.time()))
        raw = f"{self.api_key}{self.shared_secret}{timestamp}".encode("utf-8")
        signature = hashlib.sha512(raw).hexdigest()
        return {
            "Authorization": f"EAN APIKey={self.api_key},Signature={signature},timestamp={timestamp}",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/json",
        }

    def search_hotels(self, d: DestinationInsight, checkin: date, checkout: date) -> List[HotelOption]:
        if not self.is_configured():
            return []
        # Intégration live à brancher quand les accès partenaires seront activés.
        return []


# =========================================================
# Fallback demo data
# =========================================================

def generate_demo_flights(req: TripRequest, destination_airport: str) -> List[FlightOption]:
    d1 = req.departure_date
    d2 = req.return_date
    airports = req.origin_context.nearest_airports or ["BSL", "SXB", "FRA"]
    a1 = airports[0]
    a2 = airports[1] if len(airports) > 1 else airports[0]
    a3 = airports[2] if len(airports) > 2 else airports[0]
    return [
        FlightOption("demo", a1, destination_airport, "Direct Air", 280, to_dt(d1, 8, 50), to_dt(d1, 10, 55), to_dt(d2, 12, 5), to_dt(d2, 14, 10), 0, 0, None, None, True),
        FlightOption("demo", a2, destination_airport, "Value Wings", 240, to_dt(d1, 6, 40), to_dt(d1, 11, 20), to_dt(d2, 17, 45), to_dt(d2, 22, 10), 1, 1, 65, 80, False),
        FlightOption("demo", a3, destination_airport, "Premium Air", 390, to_dt(d1, 10, 30), to_dt(d1, 13, 10), to_dt(d2, 14, 25), to_dt(d2, 17, 5), 0, 0, None, None, True),
    ]


def generate_demo_hotels(checkin: date, checkout: date) -> List[HotelOption]:
    nights = max((checkout - checkin).days, 1)
    return [
        HotelOption("demo", "Central Boutique Stay", 155, 155 * nights, "Centro", 8, True, 8.9, True, "boutique"),
        HotelOption("demo", "Comfort City Rooms", 118, 118 * nights, "Centre élargi", 17, False, 8.4, True, "comfort"),
        HotelOption("demo", "Premium Terrace Hotel", 190, 190 * nights, "Historic Center", 6, True, 9.1, True, "premium"),
        HotelOption("demo", "Budget Smart Stay", 92, 92 * nights, "Périphérie proche", 28, False, 7.9, False, "budget"),
    ]


# =========================================================
# Scoring engine
# =========================================================

class TravelScorer:
    def __init__(self, request: TripRequest):
        self.request = request

    def score_destination(self, d: DestinationInsight) -> Tuple[float, List[str]]:
        prefs = self.request.preferences
        score = d.overall_leisure_score * 10
        reasons: List[str] = []
        if prefs.trip_style == "culture":
            score += (d.culture_score - 7.0) * 4
        elif prefs.trip_style == "nature":
            score += (d.nature_score - 7.0) * 4
        elif prefs.trip_style == "hiking":
            score += (d.hiking_score - 7.0) * 4
        elif prefs.trip_style == "seaside":
            score += (d.seaside_score - 7.0) * 4
        elif prefs.trip_style == "food":
            score += (d.food_score - 7.0) * 4
        elif prefs.trip_style == "romantic":
            score += (d.romance_score - 7.0) * 4
        if d.food_score >= 8.5:
            reasons.append("destination séduisante côté gastronomie")
        if d.nature_score >= 8.5:
            reasons.append("fort intérêt nature")
        if d.romance_score >= 8.4:
            reasons.append("très bien pour un voyage en couple")
        if d.culture_score >= 8.8:
            reasons.append("patrimoine urbain riche")
        return clamp(score, 0, 100), reasons[:3]

    def score_price(self, total_price: float) -> float:
        budget = self.request.preferences.budget_total_eur
        if not budget:
            return 70
        ratio = total_price / budget
        if ratio <= 0.85:
            return 96
        if ratio <= 1.0:
            return 88
        if ratio <= 1.1:
            return 74
        if ratio <= 1.25:
            return 55
        return 30

    def score_schedule(self, f: FlightOption) -> float:
        prefs = self.request.preferences
        score = 75
        if classify_time_window(f.outbound_departure) in prefs.preferred_departure_windows:
            score += 12
        else:
            score -= 8
        if classify_time_window(f.inbound_departure) in prefs.preferred_return_windows:
            score += 10
        else:
            score -= 6
        if prefs.avoid_red_eye:
            if f.outbound_departure.hour >= 22 or f.outbound_departure.hour < 5:
                score -= 12
            if f.inbound_departure.hour >= 22 or f.inbound_departure.hour < 5:
                score -= 12
        return clamp(score, 0, 100)

    def score_logistics(self, f: FlightOption, h: HotelOption) -> float:
        prefs = self.request.preferences
        score = 75
        stops = f.stops_outbound + f.stops_inbound
        score -= stops * (10 if prefs.values_direct_flights else 5)
        if prefs.avoid_tight_connections:
            for cm in [f.connection_minutes_outbound, f.connection_minutes_inbound]:
                if cm is not None and cm < prefs.min_connection_minutes:
                    score -= 15
        if prefs.wants_city_center:
            if h.walking_minutes_to_center <= 15:
                score += 8
            elif h.walking_minutes_to_center > 30:
                score -= 10
        return clamp(score, 0, 100)

    def score_hotel(self, h: HotelOption) -> float:
        prefs = self.request.preferences
        score = 65 + (h.review_score_10 - 8.0) * 15
        if prefs.wants_breakfast_included and h.breakfast_included:
            score += 8
        elif prefs.wants_breakfast_included and not h.breakfast_included:
            score -= 6
        if prefs.wants_free_cancellation and h.cancellation_flexible:
            score += 8
        if prefs.hotel_style != "any" and prefs.hotel_style == h.style:
            score += 8
        return clamp(score, 0, 100)

    def score_fatigue(self, f: FlightOption) -> float:
        prefs = self.request.preferences
        score = 85
        if f.total_air_duration_hours > prefs.max_total_travel_time_hours:
            score -= (f.total_air_duration_hours - prefs.max_total_travel_time_hours) * 8
        score -= (f.stops_outbound + f.stops_inbound) * 6
        if not f.baggage_included:
            score -= 4
        return clamp(score, 0, 100)

    def combine(self, d: DestinationInsight, f: FlightOption, h: HotelOption) -> TravelPlan:
        prefs = self.request.preferences
        total_price = f.price_eur_total + h.total_price_eur + self.request.origin_context.typical_transfer_cost_eur
        s_dest, dest_reasons = self.score_destination(d)
        s_price = self.score_price(total_price)
        s_schedule = self.score_schedule(f)
        s_log = self.score_logistics(f, h)
        s_hotel = self.score_hotel(h)
        s_fatigue = self.score_fatigue(f)
        total_weight = prefs.weight_price + prefs.weight_schedule + prefs.weight_logistics + prefs.weight_hotel + prefs.weight_fatigue + prefs.weight_destination
        score = (
            s_price * prefs.weight_price + s_schedule * prefs.weight_schedule + s_log * prefs.weight_logistics +
            s_hotel * prefs.weight_hotel + s_fatigue * prefs.weight_fatigue + s_dest * prefs.weight_destination
        ) / total_weight

        rationale = list(dict.fromkeys([
            *dest_reasons,
            "cohérent avec le budget" if s_price >= 80 else "budget à surveiller",
            "horaires agréables" if s_schedule >= 80 else "horaires perfectibles",
            "logistique fluide" if s_log >= 80 else "logistique un peu moins simple",
            "hôtel bien noté" if s_hotel >= 80 else "hôtel correct sans être exceptionnel",
        ]))[:5]

        downside = "pas de faiblesse majeure"
        if total_price > (prefs.budget_total_eur or total_price + 1):
            downside = "coût supérieur au budget cible"
        elif f.stops_outbound + f.stops_inbound > 0:
            downside = "présence d'escales"
        elif h.walking_minutes_to_center > 20:
            downside = "hôtel un peu excentré"
        elif not h.breakfast_included:
            downside = "petit-déjeuner non inclus"

        return TravelPlan(d, f, h, round(total_price, 2), round(score, 1), {
            "destination": round(s_dest, 1),
            "prix": round(s_price, 1),
            "horaires": round(s_schedule, 1),
            "logistique": round(s_log, 1),
            "hotel": round(s_hotel, 1),
            "fatigue": round(s_fatigue, 1),
        }, rationale, downside, "Meilleur équilibre")


class TravelPlanner:
    def __init__(self, request: TripRequest):
        self.request = request
        self.scorer = TravelScorer(request)
        self.sky = SkyscannerClient(get_secret("SKYSCANNER_API_KEY"))
        self.expedia = ExpediaRapidClient(get_secret("EXPEDIA_RAPID_API_KEY"), get_secret("EXPEDIA_RAPID_SHARED_SECRET"))

    def get_flights(self, destination_airport: str) -> List[FlightOption]:
        flights = self.sky.search_flights_live(self.request, destination_airport)
        return flights if flights else generate_demo_flights(self.request, destination_airport)

    def get_hotels(self, d: DestinationInsight) -> List[HotelOption]:
        hotels = self.expedia.search_hotels(d, self.request.departure_date, self.request.return_date)
        return hotels if hotels else generate_demo_hotels(self.request.departure_date, self.request.return_date)

    def best_plan_for_destination(self, d: DestinationInsight) -> Optional[TravelPlan]:
        flights = self.get_flights(d.airport_code)
        hotels = self.get_hotels(d)
        plans: List[TravelPlan] = []
        for f in flights:
            for h in hotels:
                plans.append(self.scorer.combine(d, f, h))
        plans.sort(key=lambda p: p.score, reverse=True)
        return plans[0] if plans else None


# =========================================================
# UI
# =========================================================

profile = load_profile()
st.set_page_config(page_title="Assistant Voyage V4", layout="wide")
st.title("✈️ Assistant Voyage V4")
st.caption("Outil perso puissant + architecture produit/API")

tab1, tab2, tab3, tab4 = st.tabs(["Profil", "Préférences voyage", "Comparateur destinations", "Détails destination"])

with tab1:
    st.subheader("Profil voyageur mémorisé")
    col1, col2 = st.columns(2)
    with col1:
        traveler_name = st.text_input("Nom du profil", value=profile.traveler_name)
        home_city = st.text_input("Ville de départ habituelle", value=profile.home_city)
        nearest_airports_raw = st.text_input("Aéroports favoris", value=", ".join(profile.nearest_airports))
    with col2:
        transfer_cost = st.number_input("Coût transfert aéroport (€)", min_value=0, value=int(profile.transfer_cost_eur), step=5)
        default_budget = st.number_input("Budget habituel (€)", min_value=0, value=int(profile.default_budget_eur), step=50)
        default_trip_style = st.selectbox("Style préféré", ["mixed", "culture", "nature", "hiking", "seaside", "food", "romantic"], index=["mixed", "culture", "nature", "hiking", "seaside", "food", "romantic"].index(profile.default_trip_style))
        default_pace = st.selectbox("Rythme préféré", ["slow", "balanced", "intense"], index=["slow", "balanced", "intense"].index(profile.default_pace))
        default_hotel_style = st.selectbox("Style hôtel préféré", ["budget", "comfort", "premium", "boutique", "any"], index=["budget", "comfort", "premium", "boutique", "any"].index(profile.default_hotel_style))

    if st.button("Enregistrer le profil"):
        profile = UserProfile(
            traveler_name=traveler_name,
            home_city=home_city,
            nearest_airports=[a.strip().upper() for a in nearest_airports_raw.split(",") if a.strip()],
            transfer_cost_eur=float(transfer_cost),
            default_budget_eur=float(default_budget),
            default_trip_style=default_trip_style,
            default_pace=default_pace,
            default_hotel_style=default_hotel_style,
        )
        save_profile(profile)
        st.success("Profil enregistré localement.")

with tab2:
    st.subheader("Préférences du voyage en cours")
    c1, c2, c3 = st.columns(3)
    with c1:
        departure_date = st.date_input("Date aller", value=date(2026, 6, 8))
        return_date = st.date_input("Date retour", value=date(2026, 6, 10))
        budget_total = st.number_input("Budget total (€)", min_value=0, value=int(profile.default_budget_eur), step=50)
    with c2:
        trip_style = st.selectbox("Type de voyage", ["mixed", "culture", "nature", "hiking", "seaside", "food", "romantic"], index=["mixed", "culture", "nature", "hiking", "seaside", "food", "romantic"].index(profile.default_trip_style), key="trip_style_current")
        pace = st.selectbox("Rythme", ["slow", "balanced", "intense"], index=["slow", "balanced", "intense"].index(profile.default_pace), key="pace_current")
        hotel_style = st.selectbox("Style hôtel", ["budget", "comfort", "premium", "boutique", "any"], index=["budget", "comfort", "premium", "boutique", "any"].index(profile.default_hotel_style), key="hotel_style_current")
    with c3:
        max_travel_time = st.slider("Durée max trajet (h)", 2.0, 20.0, 8.0, 0.5)
        cabin = st.selectbox("Cabine", ["economy", "premium_economy", "business", "any"], index=0)
        direct_priority = st.checkbox("Priorité vols directs", value=True)

    departure_windows = st.multiselect("Horaires aller préférés", ["morning", "midday", "afternoon", "evening"], default=["morning", "midday"])
    return_windows = st.multiselect("Horaires retour préférés", ["morning", "midday", "afternoon", "evening"], default=["afternoon", "evening"])
    avoid_red_eye = st.checkbox("Éviter vols de nuit", value=True)
    avoid_tight_connections = st.checkbox("Éviter correspondances courtes", value=True)
    breakfast = st.checkbox("Petit-déjeuner souhaité", value=False)
    free_cancel = st.checkbox("Annulation flexible souhaitée", value=True)
    notes = st.text_area("Notes", value="Voyage couple optimisé, meilleur compromis prix / confort / intérêt sur place.", height=80)

    st.markdown("**Pondérations du score**")
    w1, w2, w3, w4, w5, w6 = st.columns(6)
    with w1:
        w_price = st.slider("Prix", 0, 100, 30, 5)
    with w2:
        w_schedule = st.slider("Horaires", 0, 100, 20, 5)
    with w3:
        w_log = st.slider("Logistique", 0, 100, 15, 5)
    with w4:
        w_hotel = st.slider("Hôtel", 0, 100, 10, 5)
    with w5:
        w_fatigue = st.slider("Fatigue", 0, 100, 10, 5)
    with w6:
        w_dest = st.slider("Destination", 0, 100, 15, 5)

prefs = TravelPreferences(
    adults=2,
    budget_total_eur=float(budget_total) if budget_total > 0 else None,
    max_total_travel_time_hours=float(max_travel_time),
    preferred_departure_windows=departure_windows or ["morning", "midday"],
    preferred_return_windows=return_windows or ["afternoon", "evening"],
    avoid_red_eye=avoid_red_eye,
    avoid_tight_connections=avoid_tight_connections,
    min_connection_minutes=75,
    cabin=cabin,
    hotel_style=hotel_style,
    pace=pace,
    trip_style=trip_style,
    wants_city_center=True,
    wants_breakfast_included=breakfast,
    wants_free_cancellation=free_cancel,
    values_direct_flights=direct_priority,
    weight_price=w_price,
    weight_schedule=w_schedule,
    weight_logistics=w_log,
    weight_hotel=w_hotel,
    weight_fatigue=w_fatigue,
    weight_destination=w_dest,
)

origin = HomeContext(
    city=profile.home_city,
    country=profile.home_country,
    nearest_airports=profile.nearest_airports,
    typical_transfer_cost_eur=profile.transfer_cost_eur,
)

with tab3:
    st.subheader("Comparateur multi-destinations")
    if departure_date >= return_date:
        st.error("La date retour doit être postérieure à la date aller.")
    else:
        advisor = DestinationAdvisor()
        suggestions = advisor.suggest(departure_date, return_date, prefs, max_results=6)
        planner = TravelPlanner(TripRequest(origin, "", "", departure_date, return_date, prefs, notes))

        compare_rows = []
        plans_by_name: Dict[str, TravelPlan] = {}
        progress = st.progress(0)
        for i, (_, d) in enumerate(suggestions, start=1):
            best = planner.best_plan_for_destination(d)
            if best:
                key = f"{d.destination_city}, {d.destination_country}"
                plans_by_name[key] = best
                compare_rows.append({
                    "destination": key,
                    "score global": best.score,
                    "score destination": best.subscores["destination"],
                    "prix total": best.total_estimated_price_eur,
                    "aéroport": d.airport_code,
                    "vol": f"{best.flight.origin_airport} → {best.flight.destination_airport}",
                    "escales": best.flight.stops_outbound + best.flight.stops_inbound,
                    "hôtel": best.hotel.name,
                    "note hôtel": best.hotel.review_score_10,
                })
            progress.progress(i / len(suggestions))
        compare_df = pd.DataFrame(compare_rows).sort_values(by=["score global", "prix total"], ascending=[False, True])
        st.dataframe(compare_df, width="stretch", hide_index=True)
        st.session_state["plans_by_name"] = plans_by_name
        if not compare_df.empty:
            st.success(f"Top destination actuelle : {compare_df.iloc[0]['destination']}")

with tab4:
    st.subheader("Analyse détaillée d'une destination")
    plans_by_name = st.session_state.get("plans_by_name", {})
    if not plans_by_name:
        st.info("Passe d'abord par l'onglet Comparateur multi-destinations.")
    else:
        selected_name = st.selectbox("Destination", list(plans_by_name.keys()))
        selected_plan = plans_by_name[selected_name]
        d = selected_plan.destination

        left, right = st.columns([1.2, 1])
        with left:
            st.markdown(f"### {selected_name}")
            st.write(d.summary)
            st.markdown("**À voir**")
            for x in d.best_visits:
                st.write(f"- {x}")
            st.markdown("**Nature**")
            for x in d.best_nature_spots:
                st.write(f"- {x}")
            st.markdown("**Randonnées**")
            for x in d.best_hikes:
                st.write(f"- {x}")
        with right:
            st.dataframe(pd.DataFrame([
                {"critère": "culture", "score": d.culture_score},
                {"critère": "nature", "score": d.nature_score},
                {"critère": "randonnée", "score": d.hiking_score},
                {"critère": "bord de mer", "score": d.seaside_score},
                {"critère": "gastronomie", "score": d.food_score},
                {"critère": "romantique", "score": d.romance_score},
            ]), width="stretch", hide_index=True)

        st.markdown("### Meilleure combinaison actuelle")
        p = selected_plan
        a, b, c = st.columns([1.3, 1.2, 1])
        with a:
            st.markdown(f"**Vol** : {p.flight.airline} — {p.flight.origin_airport} → {p.flight.destination_airport}")
            st.write(f"Aller : {p.flight.outbound_departure.strftime('%d/%m %H:%M')} → {p.flight.outbound_arrival.strftime('%H:%M')}")
            st.write(f"Retour : {p.flight.inbound_departure.strftime('%d/%m %H:%M')} → {p.flight.inbound_arrival.strftime('%H:%M')}")
            st.write(f"Escales : {p.flight.stops_outbound + p.flight.stops_inbound}")
        with b:
            st.markdown(f"**Hôtel** : {p.hotel.name}")
            st.write(f"Quartier : {p.hotel.district}")
            st.write(f"Centre à pied : {p.hotel.walking_minutes_to_center} min")
            st.write(f"Note : {p.hotel.review_score_10}/10")
        with c:
            st.metric("Total estimé", f"{p.total_estimated_price_eur} €")
            for label, value in p.subscores.items():
                st.write(f"- {label}: {value}")
        st.markdown("**Pourquoi cette option ressort**")
        for r in p.rationale:
            st.write(f"- {r}")
        st.markdown(f"**Point faible principal :** {p.downside}")

st.markdown("---")
api_cols = st.columns(2)
with api_cols[0]:
    st.subheader("État API")
    planner_for_status = TravelPlanner(TripRequest(origin, "", "", departure_date, return_date, prefs, notes))
    api_df = pd.DataFrame([
        {"service": "Skyscanner Flights API", "état": "configurée" if planner_for_status.sky.is_configured() else "mode démo"},
        {"service": "Expedia Rapid Lodging API", "état": "configurée" if planner_for_status.expedia.is_configured() else "mode démo"},
    ])
    st.dataframe(api_df, width="stretch", hide_index=True)
with api_cols[1]:
    st.subheader("Fichier profil")
    st.code(str(PROFILE_PATH))
    st.markdown("**Clés attendues pour le mode live**")
    secrets_example = """SKYSCANNER_API_KEY = "..."
EXPEDIA_RAPID_API_KEY = "..."
EXPEDIA_RAPID_SHARED_SECRET = "..."
"""
    st.code(secrets_example)
