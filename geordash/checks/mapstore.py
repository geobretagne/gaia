#!/bin/env python3
# -*- coding: utf-8 -*-
# vim: ts=4 sw=4 et

from sqlalchemy import create_engine, MetaData, inspect, select, or_, and_
from sqlalchemy.engine import URL
from sqlalchemy.ext.automap import automap_base
from sqlalchemy.orm import sessionmaker
import json
import requests

from owslib.wms import WebMapService
from owslib.wfs import WebFeatureService
from owslib.wmts import WebMapTileService
from owslib.util import ServiceException

from flask import current_app as app
from celery import shared_task
from celery import Task
from celery import group
from celery.utils.log import get_task_logger
tasklogger = get_task_logger("CheckMapstore")

# solves conflicts in relationship naming ?
def name_for_collection_relationship(base, local_cls, referred_cls, constraint):
    name = referred_cls.__name__.lower()
    local_table = local_cls.__table__
    #print("local_cls={}, local_table={}, referred_cls={}, will return name={}, constraint={}".format(local_cls, local_table, referred_cls, name, constraint))
    if name in local_table.columns:
        newname = name + "_"
        print(
            "Already detected name %s present.  using %s" %
            (name, newname))
        return newname
    return name

class MapstoreChecker():
    def __init__(self, conf):
        url = URL.create(
            drivername="postgresql",
            username=conf.get('pgsqlUser'),
            host=conf.get('pgsqlHost'),
            port=conf.get('pgsqlPort'),
            password=conf.get('pgsqlPassword'),
            database=conf.get('pgsqlDatabase')
        )

        engine = create_engine(url)

# these three lines perform the "database reflection" to analyze tables and relationships
        m = MetaData(schema=conf.get('pgsqlGeoStoreSchema','mapstoregeostore'))
        Base = automap_base(metadata=m)
        Base.prepare(autoload_with=engine,name_for_collection_relationship=name_for_collection_relationship)

        # there are many tables in the database but me only directly use those
        self.Resource = Base.classes.gs_resource
        Category = Base.classes.gs_category

        Session = sessionmaker(bind=engine)
        self.session = Session()

        categories = self.session.query(Category).all()
        self.cat = dict()
        for c in categories:
            self.cat[c.name] = c.id



@shared_task()
def check_configs():
    """ check mapstore configs:
    - catalog entries provided in localConfig.json are valid
    - layers & backgrounds in new/config.json exist in services
    """
    ret = list()
    with open("/etc/georchestra/mapstore/configs/localConfig.json") as file:
        localconfig = json.load(file)
        catalogs = localconfig["initialState"]["defaultState"]["catalog"]["default"]["services"]
        ret.append({'args': "localconfig", "problems": check_catalogs(catalogs)})

    for filetype in ["new", "config"]:
        with open(f"/etc/georchestra/mapstore/configs/{filetype}.json") as file:
            s = file.read()
            mapconfig = json.loads(s)
            layers = mapconfig["map"]["layers"]
            ret.append({'args': filetype, "problems": check_layers(layers, 'MAP', filetype)})
    return ret

@shared_task()
def check_res(rescat, resid):
    msc = app.extensions["msc"]
    m = msc.session.query(msc.Resource).filter(and_(msc.Resource.category_id == msc.cat[rescat], msc.Resource.id == resid)).one()
    tasklogger.info("{} avec id {} a pour titre {}".format('la carte' if rescat == 'MAP' else 'le contexte', m.id, m.name))
    # gs_attribute is a list coming from the relationship between gs_resource and gs_attribute
    ret = dict()

    for a in m.gs_attribute:
        if a.name in ('owner', 'context', 'details', 'thumbnail'):
            if 'attribute' not in ret:
                ret['attribute'] = dict()
            ret['attribute'][a.name] = a.attribute_text
    for s in m.gs_security:
        # in the ms2-geor project, an entry with username is the owner
        if s.username is not None:
            ret['owner'] = s.username
        if s.groupname is not None:
            if 'groups' not in ret:
                ret['groups'] = dict()
            ret['groups'][s.groupname] = { 'canread': s.canread, 'canwrite': s.canwrite }

    # uses automapped attribute from relationship instead of a query
    data = json.loads(m.gs_stored_data[0].stored_data)
    ret['problems'] = list()
    if rescat == 'MAP':
        layers = data["map"]["layers"]
    else:
        layers = data["mapConfig"]["map"]["layers"]
    ret['problems'] += check_layers(layers, rescat, resid)
    if rescat == 'MAP':
        catalogs = data["catalogServices"]["services"]
    else:
        catalogs = data["mapConfig"]["catalogServices"]["services"]
    ret['problems'] += check_catalogs(catalogs)
    return ret

@shared_task
def check_resources():
    """
    called by beat scheduler, or check_mapstore_resources() route in views
    """
    msc = app.extensions["msc"]
    taskslist = list()
    for rescat in ('MAP', 'CONTEXT'):
        res = msc.session.query(msc.Resource).filter(msc.Resource.category_id == msc.cat[rescat]).all()
        for r in res:
            taskslist.append(check_res.s(rescat, r.id))
    grouptask = group(taskslist)
    groupresult = grouptask.apply_async()
    groupresult.save()
    return groupresult

def check_layers(layers, rescat, resid):
    ret = list()
    for l in layers:
        match l['type']:
            case 'wms'|'wfs'|'wmts':
                tasklogger.info('uses {} layer name {} from {} (id={})'.format(l['type'], l['name'], l['url'], l['id']))
                s = app.extensions["owscache"].get(l['type'], l['url'])
                if s.s is None:
                    ret.append(f"{l['url']} doesn't provide a {l['type']} service to look for layer {l['name']}")
                else:
                    tasklogger.debug('checking for layer presence in ows entry with ts {}'.format(s.timestamp))
                    if l['name'] not in s.contents():
                        ret.append('layer {} referenced by {} {} doesnt exist in {} service at {}'.format(l['name'], 'la carte' if rescat == 'MAP' else 'le contexte', resid, l['type'], l['url']))
            case '3dtiles':
                tasklogger.debug('uses {} from {} (id={})'.format(l['type'], l['url'], l['id']))
            case 'cog':
                tasklogger.debug(l)
            case 'empty':
                pass
            case 'osm':
                pass
            case _:
                tasklogger.debug(l)
    return ret

def check_catalogs(catalogs):
    ret = list()
    for k,c in catalogs.items():
        tasklogger.debug(f"checking catalog entry {k} type {c['type']} at {c['url']}")
        msg = f"{c['type']} catalog entry with key {k}, title {c['title']} and url {c['url']} "
        match c['type']:
            case 'wms'|'wfs'|'wmts'|'csw':
                s = app.extensions["owscache"].get(c['type'], c['url'])
                if s.s is None:
                    ret.append(msg + "doesn't seem to be an OGC service")
            case '3dtiles' | 'cog':
                try:
                    response = requests.head(c['url'], allow_redirects=True)
                    if response.status_code != 200:
                        ret.append(msg + f"doesn't seem to exist ({response.status_code}")
                except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError) as e:
                    ret.append(msg + f"Failure - Unable to establish connection: {e}.")
                except Exception as e:
                    ret.append(msg + f"Failure - Unknown error occurred: {e}.")
            case _:
                pass
    return ret

def get_name_from_ctxid(ctxid):
    msc = app.extensions["msc"]
    r = msc.session.query(msc.Resource).filter(and_(msc.Resource.category_id == msc.cat['CONTEXT'], msc.Resource.id == ctxid)).one()
    if r:
        return r.name
    return None

def get_resources_using_ows(owstype, url, layer=None):
    """ returns a set of ms resources (tuples with resource type & id)
    using a given ows layer from a given ows service type at a given url
    this awful function builds a reverse full map at each call, because i
    havent found a way to query json inside stored_data from sqlalchemy
    (and mapstore hibernate doesnt use proper psql json types on purpose?)
    if layer is not set, then it will return the set of resources using
    the given service
    """
    msc = app.extensions["msc"]
    if '~' in url:
        url = url.replace('~','/')
    layermap = dict()
    servicemap = dict()
    resources = msc.session.query(msc.Resource).filter(or_(msc.Resource.category_id == msc.cat['MAP'], msc.Resource.category_id == msc.cat['CONTEXT'])).all()
    for r in resources:
        layers = None
        data = json.loads(r.gs_stored_data[0].stored_data)
        if r.category_id == msc.cat['MAP']:
            layers = data["map"]["layers"]
            rcat = 'MAP'
        else:
            layers = data["mapConfig"]["map"]["layers"]
            rcat = 'CONTEXT'
        for l in layers:
            if 'group' in l and l["group"] == 'background':
                continue
            match l['type']:
                case 'wms'|'wfs'|'wmts':
                    lkey = (l['type'], l['url'], l['name'])
                    skey = (l['type'], l['url'])
                    val = (rcat, r.id, r.name)
                    if lkey not in layermap:
                        layermap[lkey] = set()
                    if skey not in servicemap:
                        servicemap[skey] = set()
                    layermap[lkey].add(val)
                    servicemap[skey].add(val)
                case _:
                    pass
    if layer is None:
        return servicemap.get((owstype, url), None)
    return layermap.get((owstype, url, layer), None)