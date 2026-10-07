"use client";

import { useEffect, useState } from "react";
import Map, { Source, Layer } from "react-map-gl";
import { api } from "@/lib/api";
import type { GeoThreat } from "@/types";

const MAPBOX_TOKEN = process.env.NEXT_PUBLIC_MAPBOX_TOKEN || "";

export function GeoThreatMap() {
  const [error, setError] = useState("");
  const [geoData, setGeoData] = useState<GeoThreat[]>([]);

  useEffect(() => {
    api.dashboard.geoThreatMap(30).then((res) => setGeoData(res.geo_data)).catch((cause) => setError(cause instanceof Error ? cause.message : "Could not load regional data."));
  }, []);

  const geojson = {
    type: "FeatureCollection" as const,
    features: geoData.flatMap((g) =>
      typeof g.longitude === "number" && typeof g.latitude === "number"
        && Math.abs(g.longitude) <= 180 && Math.abs(g.latitude) <= 90
        ? [{ type: "Feature" as const, properties: { threats: g.threats, max_confidence: g.max_confidence, region: g.region }, geometry: { type: "Point" as const, coordinates: [g.longitude, g.latitude] } }]
        : []),
  };
  const mappedRegions = new Set(geojson.features.map((feature) => feature.properties.region));
  const regionsWithoutCoordinates = geoData.filter((item) => !mappedRegions.has(item.region));

  return (
    <div className="h-full flex flex-col">
      <h3 className="text-sm font-semibold text-accent uppercase tracking-wider mb-2">Geographic Threat Density</h3>
      {error && <p role="alert" className="text-xs text-danger">{error}</p>}
      <div className="flex-1 rounded-lg overflow-hidden border border-card-border">
        {MAPBOX_TOKEN && geojson.features.length > 0 ? (
          <Map
            mapboxAccessToken={MAPBOX_TOKEN}
            initialViewState={{ longitude: -102, latitude: 23, zoom: 4 }}
            style={{ width: "100%", height: "100%" }}
            mapStyle="mapbox://styles/mapbox/dark-v11"
          >
            <Source id="threats" type="geojson" data={geojson}>
              <Layer
                id="heat"
                type="heatmap"
                paint={{
                  "heatmap-weight": ["get", "threats"],
                  "heatmap-intensity": 1,
                  "heatmap-color": [
                    "interpolate", ["linear"], ["heatmap-density"],
                    0, "rgba(0,0,255,0)",
                    0.2, "rgb(0,255,255)",
                    0.5, "rgb(255,255,0)",
                    1, "rgb(255,0,0)",
                  ],
                  "heatmap-radius": 20,
                }}
              />
            </Source>
          </Map>
        ) : (
          <div className="w-full h-full flex items-center justify-center bg-card text-muted text-xs font-mono">
            <div>
              <div className="mb-2 text-accent">Signals by region</div>
              <div>Records without exact coordinates are shown by region name.</div>
              <div className="mt-3 space-y-1">
                {geoData.map((g) => (
                  <div key={g.region} className="flex justify-between gap-8">
                    <span>{g.region}</span>
                    <span className="text-warning">{g.threats} signals</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>
      {MAPBOX_TOKEN && geojson.features.length > 0 && regionsWithoutCoordinates.length > 0 && (
        <div className="mt-2 max-h-20 overflow-auto text-xs text-muted">
          <div className="font-semibold">Regions without coordinates</div>
          {regionsWithoutCoordinates.map((item) => (
            <div key={item.region} className="flex justify-between gap-4">
              <span>{item.region}</span><span>{item.threats} signals</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
