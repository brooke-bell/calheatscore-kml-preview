CalHeatScore KML Data - README

CalHeatScore rates heat-related health risk for every California ZIP code on a
0-4 scale, for today and the following 6 days. These files are regenerated from
the live CalHeatScore data twice daily, at about 5:45 AM and 8:45 AM Pacific Time.

FILES

CalHeatScore_Entire_Fcst.kml
  A small file of network links to the 7 Day files. Open it in Google Earth to
  get every day of the forecast; Day 1 is shown first and the others can be
  switched on in the Places panel. Google Earth re-downloads the Day files
  automatically after each scheduled update.

CalHeatScore_Day1_Fcst.kmz ... CalHeatScore_Day7_Fcst.kmz
  One file per forecast day. Day 1 is the issue date (today), Day 2 is
  tomorrow, and so on. ZIP code polygons are colored by that day's score.

  Each placemark's popup lists the source fields below. Each placemark also
  carries machine-readable data fields (KML ExtendedData) for software that
  styles or queries by attribute:

    ZIP_CODE      ZIP code
    ISSUE_DATE    Forecast issue date (YYYY-MM-DD)
    VALID_DATE    Date this file's scores apply to (YYYY-MM-DD)
    FCST_DAY      Forecast day, 1-7
    CHS           CalHeatScore for VALID_DATE (0-4); same field name in every
                  Day file, so one style template works for all of them
    CHS_LABEL     Low, Mild, Moderate, High or Severe
    CHS_Day_0-6   Scores for all 7 days

CalHeatScore_Day1_Fcst.png ... CalHeatScore_Day7_Fcst.png
  Ready-made 1920x1080 map of each day's scores, with legend and credit
  line, for broadcasts, web stories and social posts.

CalHeatScore_7Day_Fcst.geojson
  All ZIP codes with all 7 days' scores as attributes (WGS84).

CalHeatScore_7Day_Fcst_shp.zip
  The same data as a zipped ESRI shapefile (WGS84).

CalHeatScore_latest.json
  Issue date, file update time, next scheduled update, and the valid date of
  each Day file.

SOURCE FIELDS (popups, GeoJSON and shapefile)

  ZIP_CODE        ZIP code
  DATE            Forecast issue date
  CHS_Day_0       CalHeatScore for the issue date (Day 1)
  CHS_Day_1-6     CalHeatScore for the following 6 days (Days 2-7)
  GlobalID        Feature identifier
  Shape__Area     ZIP code polygon area (KML popups only)
  Shape__Length   ZIP code polygon perimeter (KML popups only)

SCALE

  0  Low       #DEDEDE
  1  Mild      #FAE0C8
  2  Moderate  #EA8753
  3  High      #C23A00
  4  Severe    #700006

CREDIT

  Source: CalHeatScore (calheatscore.calepa.ca.gov)

Live data web service (ArcGIS REST, no key required): https://services1.arcgis.com/PCHfdHz4GlDNAhBb/arcgis/rest/services/CalHeatScore_Live_Data_for_API_Use/FeatureServer/0
More information: https://calheatscore.calepa.ca.gov/
