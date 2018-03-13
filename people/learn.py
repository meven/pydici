# coding: utf-8

"""
Module that handle predictive thing about people
@author: Sébastien Renard (sebastien.renard@digitalfox.org)
@license: AGPL v3 or newer (http://www.gnu.org/licenses/agpl-3.0.html)
"""

from datetime import date
from math import sqrt

from background_task import background

HAVE_SCIKIT = True
try:
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.neighbors import NearestNeighbors
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import MinMaxScaler
except ImportError:
    HAVE_SCIKIT = False

from django.db.models import Sum, Max, Min
from django.core.cache import cache


from staffing.models import Timesheet
from leads.models import Lead
from people.models import Consultant


CONSULTANT_SIMILARITY_MODEL_CACHE_KEY = "PYDICI_CONSULTANT_SIMILARITY_MODEL"
SIMILARITY_CONSULTANT_IDS_CACHE_KEY = "PYDICI_CONSULTANT_SIMILARITY_CONSULTANTS_IDS"

############# Features extraction ##########################

def consultant_cumulated_experience(consultant):
    features = dict()
    timesheets = Timesheet.objects.filter(consultant=consultant, mission__nature="PROD").order_by("mission__id")
    timesheets = timesheets.values_list("mission__lead").annotate(Sum("charge"), Max("working_date"))
    today = date.today()
    for lead_id, charge, end_date in timesheets:
        lead = Lead.objects.get(id=lead_id)
        weight = (today - end_date).days/30
        if weight < 1:
            weight = 1
        weight = sqrt(weight)
        weighted_charge = float(charge / weight)  # Knowledge decrease with time...
        if consultant == lead.responsible or (consultant.id,) in lead.mission_set.all().values_list("responsible"):
            weighted_charge *= 2  # It count twice when you managed the lead or mission
        for tag in lead.tags.all():
            features[tag.name] = features.get(tag.name, 0) + weighted_charge

    features[u"Profil"] = float(consultant.profil.level)
    features[consultant.company.name] = 1.0
    features[consultant.manager.trigramme] = 1.0
    experience = Timesheet.objects.filter(consultant=consultant, mission__nature="PROD").aggregate(Min("working_date"),
                                                                                      Max("working_date"))
    if experience["working_date__max"] and experience["working_date__min"]:
        features[u"experience"] = (experience["working_date__max"] - experience["working_date__min"]).days
    else:
        features[u"experience"] = 0
    return features


############# Model definition ##########################
def get_similarity_model():
    model = Pipeline([("vect", DictVectorizer(sparse=False)),
                      ("scaler", MinMaxScaler(feature_range=(0,1))),
                      ("neigh", NearestNeighbors(n_neighbors=5, metric="cosine", algorithm="brute"))])

    return model


############# Entry points for prediction ##########################
def predict_similar_consultant(consultant):
    features = consultant_cumulated_experience(consultant)
    similar_consultant = predict_similar(features, scale=True)
    return similar_consultant.exclude(pk=consultant.id)


def predict_similar(features, scale=False):
    model = compute_consultant_similarity.now()
    consultants_ids = cache.get(SIMILARITY_CONSULTANT_IDS_CACHE_KEY)
    similar_consultants = []
    if model is None:
        # cannot compute model (ex. not enough data, no scikit...)
        return []

    vect = model.named_steps["vect"]
    neigh = model.named_steps["neigh"]
    scaler = model.named_steps["scaler"]

    X = vect.transform(features)
    if scale:
        X = scaler.transform(X)
    indices = neigh.kneighbors(X, return_distance=False)

    if indices.any():
        try:
            similar_consultants_ids = [consultants_ids[indice] for indice in indices[0]]
            similar_consultants = Consultant.objects.filter(pk__in=similar_consultants_ids).select_related()
        except IndexError:
            print("While searching for consultant similarity, some consultant disapeared !")
    return similar_consultants


############# Entry points for computation ##########################

@background
def compute_consultant_similarity():
    """Compute a model to find similar consultants and cache it"""

    if not HAVE_SCIKIT:
        return

    model = cache.get(CONSULTANT_SIMILARITY_MODEL_CACHE_KEY)
    if model is None:
        consultants = Consultant.objects.filter(active=True, productive=True, subcontractor=False)
        if consultants.count() < 5:
            # Cannot learn anything with so few data
            return
        learn_features = []
        for consultant in consultants:
            learn_features.append(consultant_cumulated_experience(consultant))
        model = get_similarity_model()

        model.fit(learn_features)
        cache.set(CONSULTANT_SIMILARITY_MODEL_CACHE_KEY, model, 3600 * 24 * 7)
        cache.set(SIMILARITY_CONSULTANT_IDS_CACHE_KEY, [i.id for i in consultants], 3600 * 24 * 7)

    return model