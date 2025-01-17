# coding: utf-8

"""
Helper module that factorize some code that would not be
appropriate to live in Billing models or view
@author: Sébastien Renard (sebastien.renard@digitalfox.org)
@license: AGPL v3 or newer (http://www.gnu.org/licenses/agpl-3.0.html)
"""

import json
from os import path
import os

from django.apps import apps
from django.db.models import Sum, Count
from django.db.models.functions import TruncMonth
from django.utils.translation import ugettext as _
from django.conf import settings
from django.core.files.base import ContentFile

from core.utils import to_int_or_round, nextMonth


def get_billing_info(timesheet_data):
    """compute billing information from this timesheet data
    @:param timesheet_data: value queryset with mission, consultant and charge in days
    @:return billing information as a tuple (lead, (lead total, (mission total, billing data)) """
    Mission = apps.get_model("staffing", "Mission")
    Consultant = apps.get_model("people", "Consultant")
    billing_data = {}
    for mission_id, consultant_id, charge in timesheet_data:
        mission = Mission.objects.select_related("lead").get(id=mission_id)
        if mission.lead:
            lead = mission.lead
        else:
            # Bad data, mission with nature prod without lead... This should not happened
            continue
        consultant = Consultant.objects.get(id=consultant_id)
        rates =  mission.consultant_rates()
        if not lead in billing_data:
            billing_data[lead] = [0.0, {}]  # Lead Total and dict of mission
        if not mission in billing_data[lead][1]:
            billing_data[lead][1][mission] = [0.0, []]  # Mission Total and detail per consultant
        total = charge * rates[consultant][0]
        billing_data[lead][0] += total
        billing_data[lead][1][mission][0] += total
        billing_data[lead][1][mission][1].append(
            [consultant, to_int_or_round(charge, 2), rates[consultant][0], total])

    # Sort data
    billing_data = list(billing_data.items())
    billing_data.sort(key=lambda x: x[0].deal_id)
    return billing_data


def compute_bill(bill):
    """Compute bill amount according to its details. Should only be called by clientBill model save method"""
    if bill.state in ("0_DRAFT", "0_PROPOSED"):
        amount = 0
        amount_with_vat = 0
        for bill_detail in bill.billdetail_set.all():
            if bill_detail.amount:
                amount += bill_detail.amount
            if bill_detail.amount_with_vat:
                amount_with_vat += bill_detail.amount_with_vat

        # Add expenses
        amount += bill.expensesTotal()
        amount_with_vat += bill.expensesTotalWithTaxes()

        if amount != 0:
            bill.amount = amount
        if amount_with_vat != 0:
            bill.amount_with_vat = amount_with_vat

    # Automatically compute amount with VAT if not defined
    if not bill.amount_with_vat:
        if bill.amount:
            bill.amount_with_vat = bill.amount * (1 + bill.vat / 100)


def create_client_bill_from_timesheet(mission, month):
    """Create (and return) a bill and bill detail for given mission from timesheet of given month"""
    ClientBill = apps.get_model("billing", "clientbill")
    BillDetail = apps.get_model("billing", "billdetail")
    Consultant = apps.get_model("people", "Consultant")
    bill = ClientBill(lead=mission.lead)
    bill.save()
    rates = mission.consultant_rates()
    timesheet_data = mission.timesheet_set.filter(working_date__gte=month, working_date__lt=nextMonth(month))
    timesheet_data = timesheet_data.order_by("consultant").values("consultant").annotate(Sum("charge"))
    for i in timesheet_data:
        consultant = Consultant.objects.get(id=i["consultant"])
        billDetail =  BillDetail(bill=bill, mission=mission, month=month, consultant=consultant, quantity=i["charge__sum"], unit_price=rates[consultant][0])
        billDetail.save()
    bill.save()  # save again to update bill amount according to its details
    return bill


def create_client_bill_from_proportion(mission, proportion):
    """Create (and return) a bill and bill detail for given mission from proportion of mission total price"""
    ClientBill = apps.get_model("billing", "clientbill")
    BillDetail = apps.get_model("billing", "billdetail")
    bill = ClientBill(lead=mission.lead)
    bill.save()
    billDetail = BillDetail(bill=bill, mission=mission, quantity=proportion, unit_price=mission.price*1000)
    billDetail.save()
    bill.save()  # save again to update bill amount according to its details
    return bill


def bill_pdf_filename(bill):
    """Nice name for generated pdf file"""
    try:
        filename = "%s-%s.pdf" % (bill.bill_id, bill.lead.deal_id)
    except ValueError:
        # Incomplete bill, we still want to generate the pdf
        filename = "bill.pdf"
    return filename


def get_client_billing_control_pivotable_data(filter_on_subsidiary=None, filter_on_company=None,
                                              filter_on_lead=None, only_active=False):
    """Compute pivotable to check lead/mission billing."""
    # local import to avoid circurlar weirdness
    ClientBill = apps.get_model("billing", "ClientBill")
    BillDetail = apps.get_model("billing", "BillDetail")
    BillExpense = apps.get_model("billing", "BillExpense")
    Lead = apps.get_model("leads", "Lead")
    Expense = apps.get_model("expense", "Expense")
    Consultant = apps.get_model("people", "Consultant")

    data = []
    bill_state = ("1_SENT", "2_PAID")  # Only consider clients bills in those status
    leads = Lead.objects.all()

    if filter_on_subsidiary:
        leads = leads.filter(subsidiary=filter_on_subsidiary)
    if filter_on_company:
        leads = leads.filter(client__organisation__company=filter_on_company)
    if filter_on_lead:
        leads = leads.filter(id=filter_on_lead.id)

    if only_active:
        leads = leads.filter(mission__active=True).distinct()

    leads = leads.select_related("client__organisation__company",
                         "business_broker__company", "subsidiary")

    for lead in leads:
        lead_data = {_("deal id"): lead.deal_id,
                     _("client organisation"): str(lead.client.organisation),
                     _("client company"): str(lead.client.organisation.company),
                     _("broker"): str(lead.business_broker or _("Direct")),
                     _("subsidiary") :str(lead.subsidiary),
                     _("responsible"): str(lead.responsible),
                     _("consultant"): "-"}
        # Add legacy bills non related to specific mission (ie. not using pydici billing, just header and pdf payload)
        legacy_bills = ClientBill.objects.filter(lead=lead, state__in=bill_state).annotate(Count("billdetail")).filter(billdetail__count=0)
        for legacy_bill in legacy_bills:
            legacy_bill_data = lead_data.copy()
            legacy_bill_data[_("amount")] = - float(legacy_bill.amount or 0)
            legacy_bill_data[_("month")] = legacy_bill.creation_date.replace(day=1).isoformat()
            legacy_bill_data[_("type")] = _("Service bill")
            legacy_bill_data[_("mission")] = "-"
            mission = lead.mission_set.first()
            if mission:  # default to billing mode of first mission. Not 100% accurate...
                legacy_bill_data[_("billing mode")] = mission.get_billing_mode_display()
            data.append(legacy_bill_data)
        # Add chargeable expense
        expenses = Expense.objects.filter(lead=lead, chargeable=True)
        bill_expenses = BillExpense.objects.filter(bill__lead=lead).exclude(expense_date=None)
        for qs, label, way in ((expenses, _("Expense"), 1), (bill_expenses, _("Expense bill"), -1)):
            qs = qs.annotate(month=TruncMonth("expense_date")).order_by("month").values("month")
            for month, amount in qs.annotate(Sum("amount")).values_list("month", "amount__sum"):
                expense_data = lead_data.copy()
                expense_data[_("month")] = month.isoformat()
                expense_data[_("type")] = label
                expense_data[_("billing mode")] = _("Chargeable expense")
                expense_data[_("amount")] = float(amount) * way
                data.append(expense_data)
        # Add new-style client bills and done work per mission
        for mission in lead.mission_set.all().select_related("responsible"):
            mission_data = lead_data.copy()
            mission_data[_("mission")] = mission.short_name()
            mission_data[_("responsible")] = str(mission.responsible or mission.lead.responsible)
            mission_data[_("billing mode")] = mission.get_billing_mode_display()
            # Add fixed price bills
            if mission.billing_mode == "FIXED_PRICE":
                for billDetail in BillDetail.objects.filter(mission=mission, bill__state__in=bill_state):
                    mission_fixed_price_data = mission_data.copy()
                    mission_fixed_price_data[_("month")] = billDetail.bill.creation_date.replace(day=1).isoformat()
                    mission_fixed_price_data[_("type")] = _("Service bill")
                    mission_fixed_price_data[_("amount")] = -float(billDetail.amount or 0)
                    data.append((mission_fixed_price_data))
            # Add done work and time spent bills
            consultants = Consultant.objects.filter(timesheet__mission=mission).distinct()
            for month in mission.timesheet_set.dates("working_date", "month", order="ASC"):
                next_month = nextMonth(month)
                for consultant in consultants:
                    mission_month_consultant_data = mission_data.copy()
                    turnover = float(mission.done_work_period(month, next_month, include_external_subcontractor=True,
                                                            include_internal_subcontractor=True,
                                                            filter_on_consultant=consultant)[1])
                    mission_month_consultant_data[_("consultant")] = str(consultant)
                    mission_month_consultant_data[_("month")] = month.isoformat()
                    mission_month_consultant_data[_("amount")] = turnover
                    mission_month_consultant_data[_("type")] = _("Done work")
                    data.append(mission_month_consultant_data)
                    if mission.billing_mode == "TIME_SPENT":
                        # Add bills for time spent mission
                        mission_month_consultant_data = mission_month_consultant_data.copy()
                        billed = BillDetail.objects.filter(mission=mission, consultant=consultant,
                                                           month=month, bill__state__in=bill_state)
                        billed = float(billed.aggregate(Sum("amount"))["amount__sum"] or 0)
                        mission_month_consultant_data[_("amount")] = -billed
                        mission_month_consultant_data[_("type")] = _("Service bill")
                        data.append(mission_month_consultant_data)

    return json.dumps(data)


def generate_bill_pdf(bill, request):
    """Generate bill pdf file and update bill object with file path"""
    from billing.views import BillPdf  # Local to avoid circular import
    fake_http_request = request
    fake_http_request.method = "GET"
    response = BillPdf.as_view()(fake_http_request, bill_id=bill.id)
    pdf = response.rendered_content.read()
    filename = bill_pdf_filename(bill)
    content = ContentFile(pdf, name=filename)
    bill.bill_file.save(filename, content)
    bill.save()

def switch_bill_id(nature, dry_run=True, verbose=True):
    """Rename bill files/directories using technical bill.id instead of bill.bill_id"""
    #TODO: onetime script. Could be removed in short future.
    base_location = path.join(settings.PYDICI_ROOTDIR, "data", "bill", nature)

    if nature == "supplier":
        Bill = apps.get_model("billing", "supplierbill")
        bills = Bill.objects.all()
        funct_id = "supplier_bill_id"
    elif nature == "client":
        Bill = apps.get_model("billing", "clientbill")
        bills = Bill.objects.exclude(state__in=("0_DRAFT", "0_PROPOSED"))
        funct_id = "bill_id"
    else:
        print("Nature must be 'client' or 'supplier'")
        return

    for bill in bills:
        bill_id = getattr(bill, funct_id)
        if verbose:
            print("=" * 10)
            print("Functionnal id is %s -- Technical id is %s" % (bill_id , bill.id))
        if bill.bill_file:
            bill_path = path.join(base_location, bill.bill_file.name)
            if path.exists(bill_path):
                bill_dir = path.abspath(path.dirname((bill_path)))
                if verbose and len(os.listdir(bill_dir)) != 1:
                    print("WARNING, more than one file for this bill in path %s" % bill_dir)
                new_bill_dir = path.abspath(path.join(bill_dir, path.pardir, str(bill.id)))
                if nature == "supplier":
                    # Weird case, functionnal supplier id overlap technical one for the same month. Sic.
                    # Tmp path will be removed in second loop
                    new_bill_dir = new_bill_dir +"-tmp"
                print("About to rename %s to %s" % (bill_dir, new_bill_dir))
                if not dry_run:
                    os.rename(bill_dir, new_bill_dir)
                    bill.bill_file.name = path.join(new_bill_dir, path.basename(bill_path))
                    bill.save()
            else:
                if verbose:
                    print("WARNING, bill file does not exist (did you already moved it ? %s" % bill_path)
        elif verbose:
            print("WARNING, no file for this bill")

    if nature == "supplier":
        # remove -tmp suffix used to avoid clash between tech & funct id
        for bill in bills:
            if bill.bill_file:
                bill_path = path.join(base_location, bill.bill_file.name)
                bill_dir = path.abspath(path.dirname((bill_path)))
                new_bill_dir = path.abspath(path.join(bill_dir, path.pardir, str(bill.id)))
                print("About to rename %s to %s" % (bill_dir, new_bill_dir))
                if not dry_run:
                    os.rename(bill_dir, new_bill_dir)
                    bill.bill_file.name = path.join(new_bill_dir, path.basename(bill_path))
                    bill.save()


