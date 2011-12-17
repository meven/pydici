# coding: utf-8
"""
Pydici expense views. Http request are processed here.
@author: Sébastien Renard (sebastien.renard@digitalfox.org)
@license: AGPL v3 or newer (http://www.gnu.org/licenses/agpl-3.0.html)
"""

from datetime import date, timedelta
import mimetypes
import workflows.utils as wf
import permissions.utils as perm
from workflows.models import Transition

from django.shortcuts import render_to_response
from django.http import HttpResponseRedirect, HttpResponse
from django.utils.translation import ugettext as _
from django.core import urlresolvers
from django.template import RequestContext
from django.db.models import Q

from pydici.expense.forms import ExpenseForm
from pydici.expense.models import Expense
from pydici.people.models import Consultant
from pydici.staffing.models import Mission
from pydici.core.decorator import pydici_non_public


@pydici_non_public
def expenses(request, expense_id=None):
    """Display user expenses and expenses that he can validate"""
    if not request.user.groups.filter(name="expense_requester").exists():
        return HttpResponseRedirect(urlresolvers.reverse("forbiden"))
    try:
        consultant = Consultant.objects.get(trigramme__iexact=request.user.username)
        user_team = consultant.userTeam(excludeSelf=False)
    except Consultant.DoesNotExist:
        user_team = []

    try:
        if expense_id:
            expense = Expense.objects.get(id=expense_id)
            if not (perm.has_permission(expense, request.user, "expense_edit")
                    and (expense.user == request.user or expense.user in user_team)):
                request.user.message_set.create(message=_("You are not allowed to edit that expense"))
                expense_id = None
                expense = None
    except Expense.DoesNotExist:
        request.user.message_set.create(message=_("Expense %s does not exist" % expense_id))
        expense_id = None

    if request.method == "POST":
        if expense_id:
            form = ExpenseForm(request.POST, request.FILES, instance=expense)
        else:
            form = ExpenseForm(request.POST, request.FILES)
        if form.is_valid():
            expense = form.save(commit=False)
            if not hasattr(expense, "user"):
                # Don't update user if defined (case of expense updated by manager or adminstrator)
                expense.user = request.user
            expense.creation_date = date.today()
            expense.save()
            wf.set_initial_state(expense)
            return HttpResponseRedirect(urlresolvers.reverse("pydici.expense.views.expenses"))
    else:
        if expense_id:
            form = ExpenseForm(instance=expense)  # A form that edit current expense
        else:
            form = ExpenseForm()  # An unbound form

    # Get user expenses
    user_expenses = Expense.objects.filter(user=request.user, workflow_in_progress=True).select_related()

    if user_team:
        team_expenses = Expense.objects.filter(user__in=user_team, workflow_in_progress=True).select_related()
    else:
        team_expenses = []

    # Paymaster manage all expenses
    if perm.has_role(request.user, "expense paymaster"):
        managed_expenses = Expense.objects.filter(workflow_in_progress=True).exclude(user=request.user).select_related()
    else:
        managed_expenses = team_expenses

    # Add state and transitions to expense list
    user_expenses = [(e, e.state(), None) for e in user_expenses]  # Don't compute transitions for user exp.
    managed_expenses = [(e, e.state(), e.transitions(request.user)) for e in managed_expenses]

    # Sort expenses
    user_expenses.sort(key=lambda x: "%s-%s" % (x[1], x[0].id))  # state, then creation date
    managed_expenses.sort(key=lambda x: "%s-%s" % (x[0].user, x[1]))  # user then state

    # Prune old expense in terminal state (no more transition)
    for expense in Expense.objects.filter(workflow_in_progress=True, update_date__lt=(date.today() - timedelta(30))):
        if wf.get_state(expense).transitions.count() == 0:
            expense.workflow_in_progress = False

    return render_to_response("expense/expenses.html",
                              {"user_expenses": user_expenses,
                               "managed_expenses": managed_expenses,
                               "modify_expense": bool(expense_id),
                               "form": form,
                               "user": request.user},
                               RequestContext(request))


@pydici_non_public
def expense_receipt(request, expense_id):
    """Returns expense receipt if authorize to"""
    response = HttpResponse()
    try:
        expense = Expense.objects.get(id=expense_id)
        if expense.user == request.user or\
           perm.has_role(request.user, "expense paymaster") or\
           perm.has_role(request.user, "expense manager"):
            if expense.receipt:
                response['Content-Type'] = mimetypes.guess_type(expense.receipt.name)[0] or "application/stream"
                for chunk in expense.receipt.chunks():
                    response.write(chunk)
    except (Expense.DoesNotExist, OSError):
        pass

    return response


@pydici_non_public
def expenses_history(request):
    """Display expense history"""
    #TODO: add time range (year)
    expenses = []
    try:
        consultant = Consultant.objects.get(trigramme__iexact=request.user.username)
        user_team = consultant.userTeam()
    except Consultant.DoesNotExist:
        user_team = []

    expenses = Expense.objects.all()
    if not perm.has_role(request.user, "expense paymaster"):
        expenses = expenses.filter(Q(user=request.user) | Q(user__in=user_team))

    return render_to_response("expense/expenses_history.html",
                              {"expenses": expenses,
                               "user": request.user},
                               RequestContext(request))


@pydici_non_public
def mission_expenses(request, mission_id):
    """Page fragment that display expenses related to given mission"""
    try:
        mission = Mission.objects.get(id=mission_id)
        if mission.lead:
            expenses = Expense.objects.filter(lead=mission.lead)
        else:
            expenses = []
    except Mission.DoesNotExist:
        expenses = []
    return render_to_response("expense/expense_list.html",
                              {"expenses": expenses,
                               "user": request.user},
                               RequestContext(request))


@pydici_non_public
def update_expense_state(request, expense_id, transition_id):
    """Do workflow transition for that expense"""
    try:
        expense = Expense.objects.get(id=expense_id)
        if expense.user == request.user and not perm.has_role(request.user, "expense administrator"):
            request.user.message_set.create(message=_("You cannot manage your own expense !"))
            return HttpResponseRedirect(urlresolvers.reverse("pydici.expense.views.expenses"))
    except Expense.DoesNotExist:
        request.user.message_set.create(message=_("Expense %s does not exist" % expense_id))
        return HttpResponseRedirect(urlresolvers.reverse("pydici.expense.views.expenses"))
    try:
        transition = Transition.objects.get(id=transition_id)
    except Transition.DoesNotExist:
        request.user.message_set.create(message=_("Transition %s does not exist" % transition_id))
        return HttpResponseRedirect(urlresolvers.reverse("pydici.expense.views.expenses"))

    if wf.do_transition(expense, transition, request.user):
        request.user.message_set.create(message=_("Successfully update expense"))
    else:
        request.user.message_set.create(message=_("You cannot do this transition"))
    return HttpResponseRedirect(urlresolvers.reverse("pydici.expense.views.expenses"))
